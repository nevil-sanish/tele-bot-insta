import 'dart:async';
import 'dart:io';

import '../core/models.dart';
import '../core/text_utils.dart';
import '../data/library_db.dart';
import 'analyzer.dart';
import 'errors.dart';
import 'fetcher.dart';

/// Works through the local job queue, one reel at a time, entirely on the phone:
/// library check → fetch (or the user's file) → analyze → delete video → save.
class PipelineRunner {
  PipelineRunner({required this.db, required this.fetcher, required this.analyzer, this.tempDir});

  final LibraryDb db;
  final ReelFetcher fetcher;

  /// Built per run, so a key changed in Settings takes effect on the next reel.
  final Future<Analyzer> Function() analyzer;

  /// Downloads land here and are always deleted; files the user picked are not.
  final Directory? tempDir;

  final _changes = StreamController<void>.broadcast();
  bool _running = false;

  /// Fires whenever a job's status or the library changes (the UI listens).
  Stream<void> get changes => _changes.stream;

  /// Queue a link (or a picked file) and start working. Returns the saved reel
  /// immediately if it's already in the library.
  Reel? submit({String? url, String? filePath, String? fileKey}) {
    final shortcode = extractShortcode(url) ?? 'local_${fileKey ?? DateTime.now().millisecondsSinceEpoch}';
    final saved = db.getReel(shortcode);
    if (saved != null) return saved; // analyzed before: no AI call
    db.enqueue(shortcode: shortcode, url: url, filePath: filePath);
    _changes.add(null);
    unawaited(drain());
    return null;
  }

  /// Process queued jobs until none are left. Safe to call any time.
  Future<void> drain() async {
    if (_running) return;
    _running = true;
    try {
      for (var job = db.claimNext(); job != null; job = db.claimNext()) {
        _changes.add(null);
        await run(job);
        _changes.add(null);
      }
    } finally {
      _running = false;
    }
  }

  /// Call once at startup: resume anything the OS interrupted.
  Future<void> resume() async {
    db.requeueInterrupted();
    await drain();
  }

  Future<void> run(Job job) async {
    final cached = db.getReel(job.shortcode);
    if (cached != null) {
      db.finishJob(job.id);
      return;
    }
    FetchedReel? fetched;
    var saved = false;
    try {
      _step(job, 'Fetching…');
      if (job.filePath != null) {
        final file = File(job.filePath!);
        if (!await file.exists()) throw PipelineException('The picked video is no longer available. Pick it again.');
        fetched = FetchedReel(file: file, mimeType: mimeFor(file.path), source: job.url == null ? 'gallery' : 'shared-file');
      } else {
        fetched = await fetcher.fetch(job.url!, job.shortcode);
      }

      _step(job, 'Analyzing…');
      final result = await (await analyzer()).analyze(
        fetched.file, fetched.mimeType, fetched.caption,
        onStep: (s) => _step(job, s),
      );

      _step(job, 'Saving…');
      db.saveReel(buildReel(job, fetched, result));
      db.finishJob(job.id);
      saved = true;
    } on PipelineException catch (e) {
      db.failJob(job.id, e.message);
    } catch (e) {
      db.failJob(job.id, 'Something went wrong: $e');
    } finally {
      // Only text is kept. Downloads are always deleted; a picked/shared file's
      // cache copy is deleted once analyzed (kept on failure so Retry works).
      // Files outside the app's temp folder (the user's own) are never touched.
      final f = fetched?.file;
      final downloaded = job.filePath == null;
      if (f != null && (downloaded || saved) && _inTemp(f) && await f.exists()) await f.delete();
    }
  }

  bool _inTemp(File f) => tempDir != null && f.absolute.path.startsWith(tempDir!.absolute.path);

  void _step(Job job, String step) {
    db.setStep(job.id, step);
    _changes.add(null);
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
