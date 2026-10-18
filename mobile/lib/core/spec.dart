import 'dart:convert';

import 'package:flutter/services.dart' show rootBundle;

/// The shared analysis spec (prompt, fields, video settings, chat prompts).
/// assets/analysis_spec.json is an exact copy of the bot's spec/analysis.json,
/// so the app and the server produce the same results.
class AnalysisSpec {
  AnalysisSpec(this.raw);

  final Map<String, dynamic> raw;

  static Future<AnalysisSpec> load() async =>
      AnalysisSpec(jsonDecode(await rootBundle.loadString('assets/analysis_spec.json')) as Map<String, dynamic>);

  String get instructions => raw['instructions'] as String;
  int get captionMaxChars => raw['caption_max_chars'] as int;

  List<String> get fields => [for (final f in raw['fields'] as List) f['name'] as String];
  Set<String> get listFields => {
        for (final f in raw['fields'] as List)
          if (f['type'] == 'list') f['name'] as String
      };

  Map<String, dynamic> get gemini => raw['gemini'] as Map<String, dynamic>;
  List<String> get geminiModels => List<String>.from(gemini['models'] as List);
  double get videoFps => (gemini['video_fps'] as num).toDouble();
  int get videoMaxSeconds => gemini['video_max_seconds'] as int;
  int get inlineLimitBytes => ((gemini['inline_limit_mb'] as num) * 1024 * 1024).round();
  List<String> get thinkingLevels => List<String>.from(gemini['thinking_levels'] as List);

  String get backupNote => raw['backup']['note'] as String;
  int get transcriptMaxChars => raw['backup']['transcript_max_chars'] as int;

  Map<String, dynamic> get chat => raw['chat'] as Map<String, dynamic>;

  /// The analysis prompt, with the caption as context (same text the bot sends).
  String prompt(String? caption) {
    final c = (caption ?? '').trim();
    final shown = c.isEmpty ? '(none)' : (c.length > captionMaxChars ? c.substring(0, captionMaxChars) : c);
    return '$instructions\n\nCaption (context only):\n$shown';
  }

  /// Gemini response schema: every field required, so the model fills the full shape.
  Map<String, dynamic> responseSchema() => {
        'type': 'OBJECT',
        'properties': {
          for (final f in fields)
            f: listFields.contains(f)
                ? {
                    'type': 'ARRAY',
                    'items': {'type': 'STRING'}
                  }
                : {'type': 'STRING'}
        },
        'required': fields,
        'propertyOrdering': fields,
      };

  /// Coerce whatever a model returned into the schema's types.
  Map<String, dynamic> clean(Map<String, dynamic> raw) => {
        for (final f in fields)
          f: listFields.contains(f) ? _asList(raw[f]) : _asString(raw[f]),
      };

  static List<String> _asList(Object? v) {
    if (v == null) return [];
    if (v is String) return v.trim().isEmpty ? [] : [v.trim()];
    if (v is List) return [for (final x in v) x.toString().trim()].where((s) => s.isNotEmpty).toList();
    return [v.toString()];
  }

  static String _asString(Object? v) {
    if (v == null) return '';
    if (v is String) return v;
    return jsonEncode(v);
  }

  /// Parse a model's JSON reply (tolerates ```json fences).
  Map<String, dynamic> parseReply(String text) {
    var t = text.trim();
    if (t.startsWith('```')) {
      t = t.replaceAll('`', '').trim();
      if (t.startsWith('json')) t = t.substring(4).trim();
    }
    final decoded = jsonDecode(t);
    if (decoded is! Map<String, dynamic>) throw const FormatException('model reply is not a JSON object');
    return clean(decoded);
  }
}
