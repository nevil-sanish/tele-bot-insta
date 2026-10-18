import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:path_provider/path_provider.dart';

import 'core/config.dart';
import 'core/models.dart';
import 'core/spec.dart';
import 'data/api_client.dart';
import 'data/billing.dart';
import 'data/key_store.dart';
import 'data/library_db.dart';
import 'data/session.dart';
import 'pipeline/analyzer.dart';
import 'pipeline/chat_service.dart';
import 'pipeline/errors.dart';
import 'pipeline/fetcher.dart';
import 'pipeline/gemini.dart';
import 'pipeline/groq.dart';
import 'pipeline/ocr.dart';
import 'pipeline/runner.dart';

/// Everything the app needs, created once at startup.
///
/// With the user's own AI key, fetching, analysis and chat run on this phone
/// (free); results are shared with the server library. Without a key, the
/// server does the work and charges credits.
class AppServices {
  AppServices._(this.spec, this.db, this.keys, this.api, this.session, this.billing, this.runner);

  final AnalysisSpec spec;
  final LibraryDb db;
  final KeyStore keys;
  final ApiClient api;
  final Session session;
  final PlayBilling billing;
  final PipelineRunner runner;

  static late AppServices instance;

  static Future<AppServices> init() async {
    final spec = await AnalysisSpec.load();
    final docs = await getApplicationDocumentsDirectory();
    final temp = await getTemporaryDirectory();
    final db = LibraryDb.open('${docs.path}/reel_brain.db');
    final keys = KeyStore();
    final api = ApiClient(AppConfig.apiUrl);
    final session = Session(api, keys);
    late final AppServices services;
    final runner = PipelineRunner(
      db: db,
      fetcher: WebViewFetcher(),
      analyzer: () async => services.analyzer(),
      hasOwnKey: () async => services.hasOwnKey(),
      api: api,
      allowServerFetch: () async => (await keys.read(KeyStore.serverFetch)) != '0',
      tempDir: temp,
    );
    services = AppServices._(spec, db, keys, api, session, PlayBilling(api, session), runner);
    instance = services;
    return services;
  }

  Future<GeminiClient?> _gemini() async {
    final k = await keys.read(KeyStore.gemini);
    return k == null ? null : GeminiClient(k, spec);
  }

  Future<GroqClient?> _groq() async {
    final k = await keys.read(KeyStore.groq);
    return k == null ? null : GroqClient(k);
  }

  Future<Analyzer> analyzer() async =>
      Analyzer(spec: spec, gemini: await _gemini(), groq: await _groq(), ocr: MlKitScreenTextReader());

  Future<bool> hasOwnKey() async =>
      (await keys.read(KeyStore.gemini)) != null || (await keys.read(KeyStore.groq)) != null;

  /// Ask about one reel (or the whole library when [reel] is null).
  /// Own key: answered on the phone. Otherwise: by the server, for a fraction of a credit.
  Future<String> ask(String question, {Reel? reel}) async {
    if (await hasOwnKey()) {
      final chat = ChatService(spec: spec, db: db, gemini: await _gemini(), groq: await _groq());
      if (reel != null) return chat.askReel(reel, question);
      final (answer, hits) = await chat.askLibrary(question);
      if (hits.isEmpty) return answer;
      return '$answer\n\n${[for (var i = 0; i < hits.length; i++) '[${i + 1}] ${hits[i].title}'].join('\n')}';
    }
    if (!session.signedIn) throw PipelineException('Sign in, or add your own AI key in Settings.');
    final id = await api.ask(question, shortcode: reel?.shortcode);
    final job = await api.waitFor(id, timeout: const Duration(minutes: 2));
    if (job.failed || job.reply == null) {
      throw PipelineException("Couldn't answer right now. Your credit was refunded — try again.");
    }
    unawaitedRefresh();
    return job.reply!;
  }

  /// Credits changed on the server: update the balance shown, without blocking.
  void unawaitedRefresh() {
    session.refresh().catchError((_) {});
  }

  /// Sign-out and account deletion clear everything of this person from the phone.
  Future<void> signOut({bool deleteAccount = false}) async {
    if (deleteAccount) {
      await session.deleteAccount();
    } else {
      await session.signOut();
    }
    db.clearAll();
    runner.notify();
  }

  /// Stable id for a picked video, so picking the same file twice reuses the result.
  static Future<String> fileKey(File f) async {
    final raf = await f.open();
    try {
      final head = await raf.read(64 * 1024);
      return sha1.convert([...head, ...'${await f.length()}'.codeUnits]).toString().substring(0, 16);
    } finally {
      await raf.close();
    }
  }
}
