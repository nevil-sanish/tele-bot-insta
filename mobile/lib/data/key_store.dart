import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Small secrets and settings, encrypted on the device (Android Keystore): the
/// user's own API keys (only ever sent to Google/Groq, never to us), our session
/// token, and a cached copy of the account for offline use.
class KeyStore {
  KeyStore([FlutterSecureStorage? storage]) : _s = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _s;

  static const gemini = 'gemini_api_key';
  static const groq = 'groq_api_key';
  static const session = 'session_token';
  static const account = 'account_json';
  static const serverFetch = 'allow_server_fetch'; // '0' = never use credits to fetch

  Future<String?> read(String name) async {
    final v = (await _s.read(key: name))?.trim();
    return (v == null || v.isEmpty) ? null : v;
  }

  Future<void> write(String name, String? value) async {
    final v = value?.trim() ?? '';
    if (v.isEmpty) {
      await _s.delete(key: name);
    } else {
      await _s.write(key: name, value: v);
    }
  }
}
