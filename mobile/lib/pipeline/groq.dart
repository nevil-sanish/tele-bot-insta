import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'errors.dart';

const _base = 'https://api.groq.com/openai/v1';

/// Groq with the user's own key: Whisper speech-to-text (accepts the mp4
/// directly, so the phone needs no ffmpeg) and fast chat models.
class GroqClient {
  GroqClient(this.apiKey, {http.Client? client, this.chatModels = const ['openai/gpt-oss-120b', 'openai/gpt-oss-20b'],
      this.whisperModel = 'whisper-large-v3-turbo'})
      : _http = client ?? http.Client();

  final String apiKey;
  final List<String> chatModels;
  final String whisperModel;
  final http.Client _http;

  static const maxUploadBytes = 25 * 1024 * 1024; // Groq free-tier file limit

  Future<String> transcribe(File media, String mimeType) async {
    if (await media.length() > maxUploadBytes) throw PipelineException('Video too large for speech-to-text');
    final req = http.MultipartRequest('POST', Uri.parse('$_base/audio/transcriptions'))
      ..headers['authorization'] = 'Bearer $apiKey'
      ..fields['model'] = whisperModel
      ..fields['response_format'] = 'text'
      ..files.add(await http.MultipartFile.fromPath('file', media.path));
    final resp = await http.Response.fromStream(await _http.send(req).timeout(const Duration(seconds: 120)));
    if (resp.statusCode != 200) throw ApiException('Groq', resp.statusCode, resp.body);
    return resp.body.trim();
  }

  /// Chat completion; tries each model in turn. Returns the reply text.
  Future<String> chat(List<Map<String, String>> messages, {bool json = false, int maxTokens = 700}) async {
    ApiException? last;
    for (final model in chatModels) {
      final resp = await _http
          .post(
            Uri.parse('$_base/chat/completions'),
            headers: {'authorization': 'Bearer $apiKey', 'content-type': 'application/json'},
            body: jsonEncode({
              'model': model,
              'messages': messages,
              if (json) 'response_format': {'type': 'json_object'},
              'reasoning_effort': 'low',
              'max_completion_tokens': maxTokens,
              'temperature': json ? 0.2 : 0.3,
            }),
          )
          .timeout(const Duration(seconds: 60));
      if (resp.statusCode == 200) {
        final body = jsonDecode(resp.body) as Map<String, dynamic>;
        return ((body['choices'] as List).first['message']['content'] as String? ?? '').trim();
      }
      last = ApiException('Groq', resp.statusCode, resp.body);
      if (last.isAuth) throw last;
    }
    throw last ?? PipelineException('Groq unavailable');
  }
}
