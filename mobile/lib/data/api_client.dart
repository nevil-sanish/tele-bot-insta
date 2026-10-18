import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../core/models.dart';

/// An error from our server, with a message fit to show the user.
class ApiError implements Exception {
  ApiError(this.status, this.message);
  final int status; // 0 = network
  final String message;

  bool get outOfCredits => status == 402;
  bool get unauthorized => status == 401;
  bool get network => status == 0;

  @override
  String toString() => message;
}

/// A job on the server (a reel analysis, a question, or a fetch for own-key mode).
class ServerJob {
  ServerJob(this.json);
  final Map<String, dynamic> json;

  int get id => json['job_id'] as int;
  String get status => json['status'] as String? ?? 'queued';
  bool get done => status == 'done';
  bool get failed => status == 'failed';
  String? get error => json['error'] as String?;
  Reel? get reel => json['reel'] == null ? null : Reel.fromJson(json['reel'] as Map<String, dynamic>);
  String? get reply => json['reply'] as String?;

  /// Fetch jobs: media URLs for the phone to download and analyze.
  List<(String, String)> get media => [
        for (final m in (json['media'] as List? ?? const []))
          ((m as Map)['url'] as String, m['mime'] as String? ?? 'video/mp4')
      ];
  String? get caption => json['caption'] as String?;
  String? get author => json['author'] as String?;
}

/// The Reel Brain server API (/v1). One instance per app; the session token is set after sign-in.
class ApiClient {
  ApiClient(this.baseUrl, {http.Client? client, this.pollEvery = const Duration(seconds: 2)})
      : _http = client ?? http.Client();

  final String baseUrl;
  final http.Client _http;
  final Duration pollEvery;
  String? token;

  /// Called when the server says the session is no longer valid.
  void Function()? onUnauthorized;

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('${baseUrl.replaceAll(RegExp(r'/+$'), '')}/v1$path').replace(queryParameters: query);

  Future<dynamic> _send(String method, String path, {Object? body, Map<String, String>? query}) async {
    final req = http.Request(method, _uri(path, query))
      ..headers['content-type'] = 'application/json'
      ..headers['accept'] = 'application/json';
    if (token != null) req.headers['authorization'] = 'Bearer $token';
    if (body != null) req.body = jsonEncode(body);
    http.Response resp;
    try {
      resp = await http.Response.fromStream(await _http.send(req).timeout(const Duration(seconds: 30)));
    } on TimeoutException {
      throw ApiError(0, 'The server is taking too long. Check your connection and try again.');
    } catch (_) {
      throw ApiError(0, "Can't reach Reel Brain. Check your internet connection.");
    }
    final decoded = resp.body.isEmpty ? null : _tryJson(resp.body);
    if (resp.statusCode >= 400) {
      if (resp.statusCode == 401) onUnauthorized?.call();
      throw ApiError(resp.statusCode, _message(resp.statusCode, decoded));
    }
    return decoded;
  }

  static dynamic _tryJson(String body) {
    try {
      return jsonDecode(body);
    } catch (_) {
      return null;
    }
  }

  static String _message(int status, dynamic body) {
    final detail = body is Map ? body['detail'] : null;
    if (status == 402) return "You're out of credits. Get a plan, or add your own AI key in Settings (free).";
    if (status == 429) return detail is String ? detail : 'Too many requests. Wait a moment and try again.';
    if (detail is String) return detail;
    if (detail is List && detail.isNotEmpty) return 'The server rejected that request.';
    return 'Server error ($status). Please try again.';
  }

  // ---------------------------------------------------------------- account

  /// Exchange a Google ID token for our session; returns (account, isNew).
  Future<(Account, bool)> loginGoogle(String idToken) async {
    final j = await _send('POST', '/auth/google', body: {'id_token': idToken}) as Map<String, dynamic>;
    token = j['token'] as String;
    return (Account.fromJson(j['account'] as Map<String, dynamic>), j['new_account'] == true);
  }

  Future<Account> me() async => Account.fromJson(await _send('GET', '/me') as Map<String, dynamic>);

  Future<(String, int)> linkCode() async {
    final j = await _send('POST', '/link/code') as Map<String, dynamic>;
    return (j['code'] as String, j['expires_in'] as int);
  }

  Future<void> deleteAccount() => _send('DELETE', '/me');

  // ---------------------------------------------------------------- library

  Future<List<Reel>> library({String query = '', int limit = 100, int offset = 0}) async {
    final j = await _send('GET', '/library',
        query: {'q': query, 'limit': '$limit', 'offset': '$offset'}) as Map<String, dynamic>;
    return [for (final r in j['reels'] as List) Reel.fromJson(r as Map<String, dynamic>)];
  }

  /// Share a reel analyzed on this phone (own key). Returns the version the server keeps.
  Future<Reel> uploadResult(Reel reel) async {
    final body = {
      'title': reel.title.isEmpty ? 'Untitled reel' : reel.title,
      'summary': _cut(reel.summary, 2000),
      'key_ideas': [for (final k in reel.keyIdeas.take(10)) _cut(k, 300)],
      'on_screen_text': _cut(reel.onScreenText, 5000),
      'transcript': _cut(reel.transcript, 20000),
      'tools': [for (final t in reel.tools.take(20)) _cut(t, 100)],
      'tags': [for (final t in reel.tags.take(15)) _cut(t, 50)],
      'category': _cut(reel.category, 50),
      'caption': _cut(reel.caption, 5000),
      'author': RegExp(r'^[\w.]{0,60}$').hasMatch(reel.author) ? reel.author : '',
      'date': reel.date,
      'analysis': reel.analysis,
      'model': _cut(reel.model, 100),
    };
    final j = await _send('POST', '/reels/${reel.shortcode}/result', body: body) as Map<String, dynamic>;
    return Reel.fromJson(j);
  }

  Future<void> report(String shortcode, String reason) =>
      _send('POST', '/reels/$shortcode/report', body: {'reason': _cut(reason, 500)});

  // ---------------------------------------------------------------- jobs

  /// Server analysis with credits.
  Future<int> analyze(String url) async => (await _send('POST', '/jobs', body: {'url': url}) as Map)['job_id'] as int;

  /// Own-key mode: the server only finds the media (costs a little).
  Future<int> serverFetch(String url) async =>
      (await _send('POST', '/fetch', body: {'url': url}) as Map)['job_id'] as int;

  Future<int> ask(String question, {String? shortcode}) async => (await _send('POST', '/chat',
      body: {'question': question, 'shortcode': ?shortcode}) as Map)['job_id'] as int;

  Future<ServerJob> job(int id) async => ServerJob(await _send('GET', '/jobs/$id') as Map<String, dynamic>);

  /// Poll until the job is done or failed (or [timeout] passes).
  Future<ServerJob> waitFor(int id, {Duration timeout = const Duration(minutes: 5)}) async {
    final until = DateTime.now().add(timeout);
    while (true) {
      final j = await job(id);
      if (j.done || j.failed) return j;
      if (DateTime.now().isAfter(until)) throw ApiError(0, 'This is taking longer than usual. Check back in a bit.');
      await Future<void>.delayed(pollEvery);
    }
  }

  // ---------------------------------------------------------------- plans

  Future<List<PlanInfo>> plans() async {
    final j = await _send('GET', '/plans') as Map<String, dynamic>;
    return [for (final p in j['plans'] as List) PlanInfo.fromJson(p as Map<String, dynamic>)];
  }

  Future<Account> playPurchase(String purchaseToken) async =>
      Account.fromJson(await _send('POST', '/billing/play', body: {'purchase_token': purchaseToken}) as Map<String, dynamic>);

  static String _cut(String s, int n) => s.length > n ? s.substring(0, n) : s;
}
