import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:google_sign_in/google_sign_in.dart';

import '../core/config.dart';
import '../core/models.dart';
import 'api_client.dart';
import 'key_store.dart';

/// Who is signed in. Works offline: the token and last-known account are kept
/// in the Keystore, so the library opens without a network.
class Session extends ChangeNotifier {
  Session(this.api, this.keys) {
    api.onUnauthorized = () {
      if (account != null) signOut();
    };
  }

  final ApiClient api;
  final KeyStore keys;

  Account? account;
  bool restoring = true;
  bool _googleReady = false;

  bool get signedIn => account != null;

  /// On app start: reuse the saved session, refreshing the account when online.
  Future<void> restore() async {
    try {
      final token = await keys.read(KeyStore.session);
      if (token == null) return;
      api.token = token;
      final cached = await keys.read(KeyStore.account);
      if (cached != null) account = Account.fromJson(jsonDecode(cached) as Map<String, dynamic>);
      try {
        await refresh();
      } on ApiError catch (e) {
        if (!e.network && e.unauthorized) await signOut();
      }
    } finally {
      restoring = false;
      notifyListeners();
    }
  }

  Future<void> _ensureGoogle() async {
    if (_googleReady) return;
    await GoogleSignIn.instance.initialize(
      serverClientId: AppConfig.googleServerClientId.isEmpty ? null : AppConfig.googleServerClientId,
    );
    _googleReady = true;
  }

  /// Google sign-in → our own session token.
  Future<bool> signInWithGoogle() async {
    if (AppConfig.googleServerClientId.isEmpty) {
      throw ApiError(0, 'Google sign-in is not configured in this build (GOOGLE_SERVER_CLIENT_ID).');
    }
    await _ensureGoogle();
    final user = await GoogleSignIn.instance.authenticate();
    final idToken = user.authentication.idToken;
    if (idToken == null) throw ApiError(0, 'Google did not return a sign-in token. Please try again.');
    final (acc, isNew) = await api.loginGoogle(idToken);
    await keys.write(KeyStore.session, api.token);
    await _setAccount(acc);
    return isNew;
  }

  Future<void> refresh() async => _setAccount(await api.me());

  Future<void> _setAccount(Account acc) async {
    account = acc;
    await keys.write(KeyStore.account, jsonEncode(acc.toJson()));
    notifyListeners();
  }

  Future<void> signOut() async {
    account = null;
    api.token = null;
    await keys.write(KeyStore.session, null);
    await keys.write(KeyStore.account, null);
    try {
      await _ensureGoogle();
      await GoogleSignIn.instance.signOut();
    } catch (_) {}
    notifyListeners();
  }

  Future<void> deleteAccount() async {
    await api.deleteAccount();
    await signOut();
  }
}
