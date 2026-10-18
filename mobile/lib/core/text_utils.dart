/// Everything about captions that code (not an LLM) can do. Same rules as the
/// bot's services/caption.py.
library;

final igUrlRe = RegExp(
  r'https?://(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)[^\s]*',
  caseSensitive: false,
);
final _urlRe = RegExp(r'''https?://[^\s<>"')\]]+''');
final _hashtagRe = RegExp(r'(?<![\w#])#(\w+)', unicode: true);
final _mentionRe = RegExp(r'(?<![\w@])@([\w.]+)');

String? findIgUrl(String? text) => igUrlRe.firstMatch(text ?? '')?.group(0);

String? extractShortcode(String? url) => igUrlRe.firstMatch(url ?? '')?.group(1);

String canonicalUrl(String shortcode) => 'https://www.instagram.com/reel/$shortcode/';

List<String> extractHashtags(String? text) => _unique(_hashtagRe.allMatches(text ?? '').map((m) => m.group(1)!));

List<String> extractUrls(String? text) => _unique(
      _urlRe.allMatches(text ?? '').map((m) => m.group(0)!.replaceAll(RegExp(r'[.,;:!?]+$'), '')),
    );

List<String> extractMentions(String? text) =>
    _unique(_mentionRe.allMatches(text ?? '').map((m) => m.group(1)!.replaceAll(RegExp(r'\.+$'), '')));

List<String> _unique(Iterable<String> items) {
  final seen = <String>{};
  return [
    for (final x in items)
      if (x.isNotEmpty && seen.add(x.toLowerCase())) x
  ];
}

/// Topic tags from the AI first, then the author's hashtags, max 10 (as the bot does).
List<String> mergeTags(List<String> aiTags, List<String> hashtags) {
  final out = <String>[];
  for (final t in [...aiTags.map((t) => t.toLowerCase().replaceFirst(RegExp(r'^#'), '')), ...hashtags.map((h) => h.toLowerCase())]) {
    if (t.isNotEmpty && !out.contains(t)) out.add(t);
  }
  return out.take(10).toList();
}
