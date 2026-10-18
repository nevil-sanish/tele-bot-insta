/// Build-time settings, passed with --dart-define (see mobile/README.md):
///   flutter build apk --dart-define=API_URL=https://reel-brain.onrender.com \
///     --dart-define=GOOGLE_SERVER_CLIENT_ID=123.apps.googleusercontent.com --dart-define=BOT_USERNAME=reelbrain.ai
class AppConfig {
  static const apiUrl = String.fromEnvironment('API_URL', defaultValue: 'https://reel-brain.onrender.com');

  /// The *web* OAuth client ID: Google signs the ID token for it, and the server checks it.
  static const googleServerClientId = String.fromEnvironment('GOOGLE_SERVER_CLIENT_ID');

  /// The bot's Instagram username, for the "Link Instagram" screen.
  static const botUsername = String.fromEnvironment('BOT_USERNAME', defaultValue: 'reelbrain.ai');

  static Uri page(String path) => Uri.parse('${apiUrl.replaceAll(RegExp(r'/+$'), '')}$path');
}
