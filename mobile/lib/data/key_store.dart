import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// The user's own API keys, encrypted on the device (Android Keystore).
/// They are only ever sent to Google/Groq, never to us.
class KeyStore {
  KeyStore([FlutterSecureStorage? storage]) : _s = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _s;

  static const gemini = 'gemini_api_key';
  static const groq = 'groq_api_key';

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
