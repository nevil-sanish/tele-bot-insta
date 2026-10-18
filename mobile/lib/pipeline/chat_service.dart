import 'dart:convert';

import '../core/models.dart';
import '../core/spec.dart';
import '../data/library_db.dart';
import 'errors.dart';
import 'gemini.dart';
import 'groq.dart';

/// Q&A on the phone with the user's key: about one reel (reel data + last 4
/// turns), or across the library (top full-text matches). Groq if set
/// (fast, separate quota), otherwise Gemini's lightest model.
class ChatService {
  ChatService({required this.spec, required this.db, this.groq, this.gemini});

  final AnalysisSpec spec;
  final LibraryDb db;
  final GroqClient? groq;
  final GeminiClient? gemini;

  int get _turns => spec.chat['history_turns'] as int;
  int get _budget => spec.chat['prompt_char_budget'] as int;

  Future<String> askReel(Reel reel, String question) async {
    final history = db.chatHistory(reel.shortcode);
    final historyChars = history.fold<int>(0, (n, m) => n + m['content']!.length);
    final system = spec.chat['reel_system'] as String;
    final budget = [2000, _budget - historyChars - question.length - system.length].reduce((a, b) => a > b ? a : b);
    final answer = await _complete([
      {'role': 'system', 'content': '$system\n\nReel data:\n${reelContext(reel, budget)}'},
      ...history,
      {'role': 'user', 'content': _cut(question, 2000)},
    ]);
    final updated = [
      ...history,
      {'role': 'user', 'content': _cut(question, 1000)},
      {'role': 'assistant', 'content': _cut(answer, 1500)},
    ];
    db.saveChatHistory(reel.shortcode, updated.length > _turns * 2 ? updated.sublist(updated.length - _turns * 2) : updated);
    return answer;
  }

  Future<(String, List<Reel>)> askLibrary(String question) async {
    final hits = db.search(question, limit: spec.chat['search_top_k'] as int);
    if (hits.isEmpty) return ("I couldn't find any saved reels matching that. Try different keywords.", <Reel>[]);
    final per = (_budget - 1000) ~/ hits.length;
    final blocks = [
      for (var i = 0; i < hits.length; i++)
        _cut('[${i + 1}] ${hits[i].title} (by ${hits[i].author.isEmpty ? 'unknown' : hits[i].author}; '
            'tags: ${hits[i].tags.join(', ')})\n${hits[i].summary}', per)
    ];
    final answer = await _complete([
      {'role': 'system', 'content': spec.chat['library_system'] as String},
      {'role': 'user', 'content': 'Reels:\n\n${blocks.join('\n\n')}\n\nQuestion: ${_cut(question, 1000)}'},
    ]);
    return (answer, hits);
  }

  Future<String> _complete(List<Map<String, String>> messages) async {
    if (groq != null) return groq!.chat(messages);
    if (gemini != null) {
      final system = messages.first['content']!;
      final convo = messages.skip(1).map((m) => '${m['role'] == 'user' ? 'User' : 'Assistant'}: ${m['content']}').join('\n\n');
      final reply = await gemini!.generate(
        spec.geminiModels.last,
        [{'text': '$system\n\n$convo\n\nAssistant:'}],
        json: false,
        media: false,
      );
      return reply.text.trim();
    }
    throw PipelineException('Add a Gemini or Groq key in Settings to chat.');
  }

  /// Compact JSON of the reel, cutting the long text fields first (same as the bot).
  static String reelContext(Reel reel, int budget) {
    final data = <String, dynamic>{
      'title': reel.title, 'author': reel.author, 'link': reel.link, 'summary': reel.summary,
      'key_ideas': reel.keyIdeas, 'tools': reel.tools, 'urls': reel.urls, 'tags': reel.tags,
      'category': reel.category, 'on_screen_text': reel.onScreenText, 'transcript': reel.transcript,
      'caption': reel.caption,
    };
    for (final key in ['caption', 'transcript', 'on_screen_text']) {
      final over = jsonEncode(data).length - budget;
      if (over <= 0) break;
      final value = data[key] as String;
      final keep = value.length - over - 20;
      data[key] = keep > 0 ? '${value.substring(0, keep)}…' : (value.isEmpty ? '' : '…');
    }
    final text = jsonEncode(data);
    return text.length > budget ? text.substring(0, budget) : text;
  }

  static String _cut(String s, int n) => s.length > n ? s.substring(0, n) : s;
}
