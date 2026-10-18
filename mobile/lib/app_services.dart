import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:path_provider/path_provider.dart';

import 'core/spec.dart';
import 'data/key_store.dart';
import 'data/library_db.dart';
import 'pipeline/analyzer.dart';
import 'pipeline/chat_service.dart';
import 'pipeline/fetcher.dart';
import 'pipeline/gemini.dart';
import 'pipeline/groq.dart';
import 'pipeline/ocr.dart';
import 'pipeline/runner.dart';

/// Everything the app needs, created once at startup. There is no backend:
/// the database, the queue and the AI calls all live on this phone.
class AppServices {
  AppServices._(this.spec, this.db, this.keys, this.runner);

  final AnalysisSpec spec;
  final LibraryDb db;
  final KeyStore keys;
  final PipelineRunner runner;

  static late AppServices instance;

  static Future<AppServices> init() async {
    final spec = await AnalysisSpec.load();
    final docs = await getApplicationDocumentsDirectory();
    final temp = await getTemporaryDirectory();
    final db = LibraryDb.open('${docs.path}/reel_brain.db');
    final keys = KeyStore();
    late final AppServices services;
    final runner = PipelineRunner(
      db: db,
      fetcher: WebViewFetcher(),
      analyzer: () async => services.analyzer(),
      tempDir: temp,
    );
    services = AppServices._(spec, db, keys, runner);
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

  Future<ChatService> chat() async => ChatService(spec: spec, db: db, gemini: await _gemini(), groq: await _groq());

  Future<bool> hasAnyKey() async =>
      (await keys.read(KeyStore.gemini)) != null || (await keys.read(KeyStore.groq)) != null;

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
