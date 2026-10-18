import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:http/http.dart' as http;

import '../core/models.dart';
import '../core/text_utils.dart';
import '../data/api_client.dart';
import '../data/library_db.dart';
import 'analyzer.dart';
import 'errors.dart';
import 'fetcher.dart';

/// Works through the local job queue, one reel at a time.
///
/// - **Own key (free):** fetch on the phone (logged-out page) → analyze with the
///   user's Gemini/Groq key → delete the video → save → share the result with the
///   server (the server keeps its own analysis if it already has one).
///   If the phone can't open the reel, the server finds the media (3 credits).
/// - **No key (credits):** the server analyzes the reel; the phone polls for it.
class PipelineRunner {
  PipelineRunner({
    required this.db,
    required this.fetcher,
    required this.analyzer,
    required this.hasOwnKey,
    this.api,
    this.allowServerFetch,
    this.tempDir,
    http.Client? downloader,
  }) : _http = downloader ?? http.Client();

  final LibraryDb db;
  final ReelFetcher fetcher;

  /// Built per run, so a key changed in Settings takes effect on the next reel.
  final Future<Analyzer> Function() analyzer;
  final Future<bool> Function() hasOwnKey;

  /// Null when signed out (tests, or before sign-in): own-key mode only, nothing shared.
  final ApiClient? api;
  final Future<bool> Function()? allowServerFetch;

  /// Downloads land here and are always deleted; files the user picked are not.
  final Directory? tempDir;
  final http.Client _http;

  final _changes = StreamController<void>.broadcast();
  bool _running = false;

  /// Fires whenever a job's status or the library changes (the UI listens).
  Stream<void> get changes => _changes.stream;

  void notify() => _changes.add(null);

  /// Queue a link (or a picked file) and start working. Returns the saved reel
  /// immediately if it's already in the library on this phone.
  Reel? submit({String? url, String? filePath, String? fileKey}) {
    final shortcode = extractShortcode(url) ?? 'app_${fileKey ?? _randomKey()}';
    final saved = db.getReel(shortcode);
    if (saved != null) return saved;
    db.enqueue(shortcode: shortcode, url: url, filePath: filePath);
    notify();
    unawaited(drain());
    return null;
  }

  static String _randomKey() {
    final r = Random.secure();
    return List.generate(16, (_) => r.nextInt(16).toRadixString(16)).join();
  }

  /// Process queued jobs until none are left. Safe to call any time.
  Future<void> drain() async {
    if (_running) return;
    _running = true;
    try {
      for (var job = db.claimNext(); job != null; job = db.claimNext()) {
        notify();
        await run(job);
        notify();
      }
    } finally {
      _running = false;
    }
  }

  /// Call once at startup: resume anything the OS interrupted, then share leftovers.
  Future<void> resume() async {
    db.requeueInterrupted();
    await drain();
    await uploadPending();
  }

  Future<void> run(Job job) async {
    if (db.getReel(job.shortcode) != null) {
      db.finishJob(job.id);
      return;
    }
    try {
      if (await hasOwnKey()) {
        await _runOnPhone(job);
      } else {
        await _runOnServer(job);
      }
      db.finishJob(job.id);
    } on PipelineException catch (e) {
      db.failJob(job.id, e.message);
    } on ApiError catch (e) {
      db.failJob(job.id, e.message);
    } catch (e) {
      db.failJob(job.id, 'Something went wrong: $e');
    }
  }

  // ---------------------------------------------------------------- credits mode

  Future<void> _runOnServer(Job job) async {
    final api = this.api;
    if (api == null) throw PipelineException('Sign in, or add your own AI key in Settings.');
    if (job.url == null) {
      throw PipelineException('Videos from your phone are analyzed with your own AI key. '
          'Add one in Settings (free), or share the reel link instead.');
    }
    _step(job, 'Queued on the server…');
    final id = job.serverJobId ?? await api.analyze(job.url!);
    db.setServerJob(job.id, id);
    _step(job, 'Analyzing on the server…');
    final result = await api.waitFor(id);
    if (result.failed || result.reel == null) {
      db.setServerJob(job.id, null);
      throw PipelineException(_friendly(result.error));
    }
    db.saveReel(result.reel!);
  }

  static String _friendly(String? error) {
    final e = error ?? '';
    if (e.startsWith('fetch') || e.startsWith('download')) {
      return "Couldn't get this reel. Save the video and pick it from your gallery.";
    }
    if (e.startsWith('analysis')) return 'The AI service is busy. Your credits were refunded — retry in a bit.';
    return 'Analysis failed. Your credits were refunded — please retry.';
  }

  // ---------------------------------------------------------------- own-key mode

  Future<void> _runOnPhone(Job job) async {
    FetchedReel? fetched;
    var saved = false;
    try {
      _step(job, 'Fetching…');
      if (job.filePath != null) {
        final file = File(job.filePath!);
        if (!await file.exists()) throw PipelineException('The picked video is no longer available. Pick it again.');
        fetched = FetchedReel(file: file, mimeType: mimeFor(file.path), source: 'phone');
      } else {
        try {
          fetched = await fetcher.fetch(job.url!, job.shortcode);
        } on PipelineException {
          final fromServer = await _serverFetch(job);
          if (fromServer == null) rethrow;
          if (fromServer is Reel) {
            db.saveReel(fromServer); // already analyzed by someone: free
            saved = true;
            return;
          }
          fetched = fromServer as FetchedReel;
        }
      }

      _step(job, 'Analyzing…');
      final result = await (await analyzer()).analyze(
        fetched.file, fetched.mimeType, fetched.caption,
        onStep: (s) => _step(job, s),
      );

      _step(job, 'Saving…');
      final reel = buildReel(job, fetched, result);
      db.saveReel(reel, pendingUpload: api != null);
      saved = true;
      await _share(reel);
    } finally {
      // Only text is kept. Downloads are always deleted; a picked/shared file's
      // cache copy is deleted once analyzed (kept on failure so Retry works).
      // Files outside the app's temp folder (the user's own) are never touched.
      final f = fetched?.file;
      final downloaded = job.filePath == null;
      if (f != null && (downloaded || saved) && _inTemp(f) && await f.exists()) await f.delete();
    }
  }

  /// The phone couldn't open the reel: ask the server to find the media (if allowed).
  /// Returns a FetchedReel to analyze here, a Reel if the server already has it, or null.
  Future<Object?> _serverFetch(Job job) async {
    final api = this.api;
    if (api == null || job.url == null || !(await allowServerFetch?.call() ?? true)) return null;
    _step(job, 'Fetching via server…');
    final id = job.serverJobId ?? await api.serverFetch(job.url!);
    db.setServerJob(job.id, id);
    final result = await api.waitFor(id);
    db.setServerJob(job.id, null);
    if (result.failed) throw PipelineException("Couldn't get this reel. Save the video and pick it from your gallery.");
    if (result.reel != null) return result.reel;
    if (result.media.isEmpty) return null;
    final (url, mime) = result.media.first;
    final file = await _download(url, mime.startsWith('image/') ? '.jpg' : '.mp4');
    return FetchedReel(file: file, mimeType: mime, caption: result.caption, author: result.author, source: 'apify');
  }

  Future<File> _download(String url, String ext, {int maxBytes = 100 * 1024 * 1024}) async {
    final dir = tempDir ?? Directory.systemTemp;
    final file = File('${dir.path}/reel_${DateTime.now().microsecondsSinceEpoch}$ext');
    final resp = await _http.send(http.Request('GET', Uri.parse(url))).timeout(const Duration(seconds: 30));
    if (resp.statusCode != 200) throw PipelineException('Download failed (HTTP ${resp.statusCode})');
    final sink = file.openWrite();
    var size = 0;
    try {
      await for (final chunk in resp.stream) {
        size += chunk.length;
        if (size > maxBytes) throw PipelineException('Video is larger than ${maxBytes ~/ (1024 * 1024)} MB');
        sink.add(chunk);
      }
    } catch (_) {
      await sink.close();
      await file.delete();
      rethrow;
    }
    await sink.close();
    return file;
  }

  /// Share a phone-made result; the server's version (if it already had one) replaces ours.
  Future<void> _share(Reel reel) async {
    final api = this.api;
    if (api == null) return;
    try {
      db.saveReel(await api.uploadResult(reel));
    } on ApiError {
      // Offline or rate-limited: stays marked pending and is retried by uploadPending().
    }
  }

  /// Retry sharing results made while offline.
  Future<void> uploadPending() async {
    if (api == null) return;
    for (final reel in db.pendingUploads()) {
      await _share(reel);
    }
    notify();
  }

  /// Pull the account's library from the server (reels shared with the Instagram bot
  /// show up here too). Local results not yet shared are kept and uploaded.
  Future<int> sync() async {
    final api = this.api;
    if (api == null) return 0;
    await uploadPending();
    final now = DateTime.now().millisecondsSinceEpoch;
    var offset = 0;
    while (true) {
      final page = await api.library(limit: 100, offset: offset);
      for (var i = 0; i < page.length; i++) {
        db.saveReel(page[i], savedAt: now - offset - i); // keep the server's newest-first order
      }
      offset += page.length;
      if (page.length < 100) break;
    }
    notify();
    return offset;
  }

  bool _inTemp(File f) => tempDir != null && f.absolute.path.startsWith(tempDir!.absolute.path);

  void _step(Job job, String step) {
    db.setStep(job.id, step);
    notify();
  }

  static String mimeFor(String path) {
    final p = path.toLowerCase();
    if (p.endsWith('.jpg') || p.endsWith('.jpeg')) return 'image/jpeg';
    if (p.endsWith('.png')) return 'image/png';
    if (p.endsWith('.webp')) return 'image/webp';
    if (p.endsWith('.mov')) return 'video/quicktime';
    if (p.endsWith('.webm')) return 'video/webm';
    return 'video/mp4';
  }

  static Reel buildReel(Job job, FetchedReel fetched, AnalysisResult result) {
    final d = result.data;
    final caption = fetched.caption ?? '';
    final hashtags = extractHashtags(caption);
    List<String> list(String k) => List<String>.from(d[k] as List);
    final title = (d['title'] as String).trim();
    final ideas = list('key_ideas');
    return Reel(
      shortcode: job.shortcode,
      link: job.url == null ? '' : canonicalUrl(job.shortcode),
      title: title.isEmpty ? 'Untitled reel' : title,
      summary: d['summary'] as String,
      keyIdeas: ideas.length > 5 ? ideas.sublist(0, 5) : ideas,
      onScreenText: d['on_screen_text'] as String,
      transcript: d['transcript'] as String,
      tools: list('tools'),
      tags: mergeTags(list('tags'), hashtags),
      category: (d['category'] as String).toLowerCase(),
      caption: caption,
      hashtags: hashtags,
      urls: extractUrls(caption),
      author: fetched.author ?? '',
      analysis: result.mode,
      source: fetched.source,
      model: result.model,
    );
  }

  void dispose() => _changes.close();
}
