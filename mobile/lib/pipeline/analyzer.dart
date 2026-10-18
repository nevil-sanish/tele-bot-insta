import 'dart:convert';
import 'dart:io';

import '../core/spec.dart';
import 'errors.dart';
import 'gemini.dart';
import 'groq.dart';

/// Reads on-screen text from a video (on-device OCR). Abstract so tests can fake it.
abstract class OnScreenTextReader {
  Future<String> read(File video);
}

class AnalysisResult {
  AnalysisResult(this.data, this.mode, this.model);
  final Map<String, dynamic> data; // cleaned to the spec's fields
  final String mode; // full / audio-only / caption-only
  final String model;
}

/// One heavy AI call per reel, with fallbacks, all from the phone:
/// 1. Gemini sees the video (each model in turn; quota or overload → next model).
/// 2. Backup: Groq Whisper on the video + on-device OCR, then a text model fills the same JSON.
/// 3. Caption only, if there is nothing else.
class Analyzer {
  Analyzer({required this.spec, this.gemini, this.groq, this.ocr});

  final AnalysisSpec spec;
  final GeminiClient? gemini;
  final GroqClient? groq;
  final OnScreenTextReader? ocr;

  Future<AnalysisResult> analyze(File media, String mimeType, String? caption,
      {void Function(String step)? onStep}) async {
    final errors = <String>[];
    if (gemini != null) {
      try {
        return await _withGemini(media, mimeType, caption);
      } catch (e) {
        errors.add('Gemini: $e');
        if (e is ApiException && e.isAuth) throw PipelineException('Your Gemini key was rejected. Check it in Settings.');
      }
    }
    onStep?.call('Analyzing (backup)…');
    try {
      return await _backup(media, mimeType, caption);
    } catch (e) {
      errors.add('Backup: $e');
    }
    throw PipelineException(errors.isEmpty ? 'No AI key set. Add one in Settings.' : errors.join(' | '));
  }

  Future<AnalysisResult> _withGemini(File media, String mimeType, String? caption) async {
    final part = await gemini!.mediaPart(media, mimeType);
    final errors = <String>[];
    try {
      for (final model in spec.geminiModels) {
        try {
          final reply = await gemini!.generate(model, [part.part, {'text': spec.prompt(caption)}]);
          return AnalysisResult(spec.parseReply(reply.text), 'full', model);
        } catch (e) {
          if (e is ApiException && e.isAuth) rethrow;
          errors.add('$model: $e'); // quota, overload, timeout, bad JSON: next model
        }
      }
    } finally {
      if (part.uploadedName != null) await gemini!.deleteUpload(part.uploadedName!);
    }
    throw PipelineException(errors.join('; '));
  }

  Future<AnalysisResult> _backup(File media, String mimeType, String? caption) async {
    final isVideo = mimeType.startsWith('video/');
    var transcript = '';
    var screenText = '';
    if (isVideo && groq != null) {
      try {
        transcript = await groq!.transcribe(media, mimeType);
      } catch (_) {}
    }
    if (isVideo && ocr != null) {
      try {
        screenText = await ocr!.read(media);
      } catch (_) {}
    }
    if (transcript.isEmpty && screenText.isEmpty && (caption ?? '').trim().isEmpty) {
      throw PipelineException('No speech, on-screen text or caption to analyze');
    }
    final schema = jsonEncode({for (final f in spec.fields) f: spec.listFields.contains(f) ? [] : ''});
    final t = transcript.length > spec.transcriptMaxChars ? transcript.substring(0, spec.transcriptMaxChars) : transcript;
    final user = '${spec.prompt(caption)}\n\n${spec.backupNote}\nSchema: $schema\n\n'
        'Speech transcript:\n${t.isEmpty ? '(no speech)' : t}\n\n'
        'On-screen text (OCR):\n${screenText.isEmpty ? '(none)' : screenText}';

    Map<String, dynamic> data;
    String model;
    if (groq != null) {
      data = spec.parseReply(await groq!.chat([
        {'role': 'system', 'content': 'Return only a JSON object.'},
        {'role': 'user', 'content': user},
      ], json: true, maxTokens: 1500));
      model = 'groq';
    } else if (gemini != null) {
      // Text-only call (cheap), on the lightest model in the list.
      model = spec.geminiModels.last;
      final reply = await gemini!.generate(model, [{'text': user}], media: false);
      data = spec.parseReply(reply.text);
    } else {
      throw PipelineException('No AI key set. Add one in Settings.');
    }
    if ((data['transcript'] as String).isEmpty && transcript.isNotEmpty) {
      data['transcript'] = transcript.length > 3000 ? transcript.substring(0, 3000) : transcript;
    }
    if ((data['on_screen_text'] as String).isEmpty && screenText.isNotEmpty) data['on_screen_text'] = screenText;
    final mode = transcript.isNotEmpty ? 'audio-only' : 'caption-only';
    return AnalysisResult(data, mode, model);
  }
}
