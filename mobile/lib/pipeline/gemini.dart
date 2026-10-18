import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../core/spec.dart';
import 'errors.dart';

const _base = 'https://generativelanguage.googleapis.com';

/// Gemini over REST with the user's own key. Same settings as the bot:
/// fps 0.5, LOW media resolution, first 90 s, cheapest thinking level first.
class GeminiClient {
  GeminiClient(this.apiKey, this.spec, {http.Client? client}) : _http = client ?? http.Client();

  final String apiKey;
  final AnalysisSpec spec;
  final http.Client _http;

  /// model -> index into [_thinkingOptions] that worked, so later reels skip failed tries.
  final Map<String, int> _thinkingOk = {};

  Map<String, String> get _headers => {'x-goog-api-key': apiKey, 'content-type': 'application/json'};

  List<Map<String, dynamic>?> get _thinkingOptions => [
        for (final level in spec.thinkingLevels) {'thinkingLevel': level},
        {'thinkingBudget': 0},
        null,
      ];

  Map<String, dynamic> _videoMetadata() => {
        'fps': spec.videoFps,
        'startOffset': '0s',
        'endOffset': '${spec.videoMaxSeconds}s',
      };

  /// The media part: inline for small files, Files API upload otherwise.
  Future<MediaPart> mediaPart(File file, String mimeType) async {
    final size = await file.length();
    final isVideo = mimeType.startsWith('video/');
    if (size <= spec.inlineLimitBytes) {
      return MediaPart({
        'inlineData': {'mimeType': mimeType, 'data': base64Encode(await file.readAsBytes())},
        if (isVideo) 'videoMetadata': _videoMetadata(),
      });
    }
    final uploaded = await _upload(file, mimeType, size);
    return MediaPart({
      'fileData': {'mimeType': mimeType, 'fileUri': uploaded.uri},
      if (isVideo) 'videoMetadata': _videoMetadata(),
    }, uploadedName: uploaded.name);
  }

  /// Build the generateContent request body (exposed for tests).
  Map<String, dynamic> buildRequest(List<Map<String, dynamic>> parts, Map<String, dynamic>? thinking,
      {bool json = true, bool media = true}) {
    return {
      'contents': [
        {'role': 'user', 'parts': parts}
      ],
      'generationConfig': {
        if (media) 'mediaResolution': spec.gemini['media_resolution'],
        if (json) 'responseMimeType': 'application/json',
        if (json) 'responseSchema': spec.responseSchema(),
        'maxOutputTokens': spec.gemini['max_output_tokens'],
        'temperature': spec.gemini['temperature'],
        if (thinking != null) 'thinkingConfig': thinking,
      },
    };
  }

  /// One analysis call on one model. Steps down thinking levels the model rejects.
  Future<GeminiReply> generate(String model, List<Map<String, dynamic>> parts,
      {bool json = true, bool media = true}) async {
    final options = _thinkingOptions;
    ApiException? last;
    for (var i = _thinkingOk[model] ?? 0; i < options.length; i++) {
      final resp = await _http
          .post(
            Uri.parse('$_base/v1beta/models/$model:generateContent'),
            headers: _headers,
            body: jsonEncode(buildRequest(parts, options[i], json: json, media: media)),
          )
          .timeout(const Duration(seconds: 120));
      if (resp.statusCode == 200) {
        _thinkingOk[model] = i;
        return GeminiReply.fromJson(jsonDecode(resp.body) as Map<String, dynamic>, model);
      }
      last = ApiException('Gemini', resp.statusCode, resp.body);
      // A 400 usually means this model rejects this thinking setting: try the next one.
      if (!(last.isBadRequest && options[i] != null)) throw last;
    }
    throw last ?? PipelineException('Gemini call failed');
  }

  Future<_UploadedFile> _upload(File file, String mimeType, int size) async {
    final start = await _http.post(
      Uri.parse('$_base/upload/v1beta/files'),
      headers: {
        ..._headers,
        'X-Goog-Upload-Protocol': 'resumable',
        'X-Goog-Upload-Command': 'start',
        'X-Goog-Upload-Header-Content-Length': '$size',
        'X-Goog-Upload-Header-Content-Type': mimeType,
      },
      body: jsonEncode({
        'file': {'display_name': 'reel'}
      }),
    );
    final uploadUrl = start.headers['x-goog-upload-url'];
    if (start.statusCode != 200 || uploadUrl == null) throw ApiException('Gemini upload', start.statusCode, start.body);
    final done = await _http.post(
      Uri.parse(uploadUrl),
      headers: {'X-Goog-Upload-Offset': '0', 'X-Goog-Upload-Command': 'upload, finalize'},
      body: await file.readAsBytes(),
    );
    if (done.statusCode != 200) throw ApiException('Gemini upload', done.statusCode, done.body);
    var info = (jsonDecode(done.body) as Map<String, dynamic>)['file'] as Map<String, dynamic>;
    // Videos need a few seconds of server-side processing before they can be used.
    for (var i = 0; i < 60 && info['state'] == 'PROCESSING'; i++) {
      await Future<void>.delayed(const Duration(seconds: 2));
      final r = await _http.get(Uri.parse('$_base/v1beta/${info['name']}'), headers: _headers);
      if (r.statusCode != 200) throw ApiException('Gemini upload', r.statusCode, r.body);
      info = jsonDecode(r.body) as Map<String, dynamic>;
    }
    if (info['state'] == 'FAILED') throw PipelineException('Gemini could not process the video');
    return _UploadedFile(info['name'] as String, info['uri'] as String);
  }

  Future<void> deleteUpload(String name) async {
    try {
      await _http.delete(Uri.parse('$_base/v1beta/$name'), headers: _headers);
    } catch (_) {}
  }
}

class MediaPart {
  MediaPart(this.part, {this.uploadedName});
  final Map<String, dynamic> part;
  final String? uploadedName; // Files API upload to delete afterwards
}

class _UploadedFile {
  _UploadedFile(this.name, this.uri);
  final String name;
  final String uri;
}

class GeminiReply {
  GeminiReply(this.text, this.model, this.inputTokens, this.outputTokens);

  final String text;
  final String model;
  final int inputTokens;
  final int outputTokens;

  factory GeminiReply.fromJson(Map<String, dynamic> j, String model) {
    final candidates = j['candidates'] as List? ?? const [];
    final parts = candidates.isEmpty ? const [] : (candidates.first['content']?['parts'] as List? ?? const []);
    final text = [
      for (final p in parts)
        if (p['thought'] != true && p['text'] != null) p['text'] as String
    ].join();
    if (text.isEmpty) throw PipelineException('Gemini returned no text (${candidates.isEmpty ? 'blocked' : 'empty'})');
    final usage = j['usageMetadata'] as Map<String, dynamic>? ?? const {};
    return GeminiReply(text, model, (usage['promptTokenCount'] as num?)?.toInt() ?? 0,
        (usage['candidatesTokenCount'] as num?)?.toInt() ?? 0);
  }
}
