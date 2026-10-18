/// A saved reel: text only (the video is deleted after analysis). Field names
/// match the bot's `reels` table so libraries can be synced later.
class Reel {
  Reel({
    required this.shortcode,
    this.link = '',
    this.title = '',
    this.summary = '',
    this.keyIdeas = const [],
    this.onScreenText = '',
    this.transcript = '',
    this.tools = const [],
    this.tags = const [],
    this.category = '',
    this.caption = '',
    this.hashtags = const [],
    this.urls = const [],
    this.author = '',
    this.date,
    this.analysis = 'full',
    this.source = '',
    this.model = '',
  });

  final String shortcode;
  final String link;
  final String title;
  final String summary;
  final List<String> keyIdeas;
  final String onScreenText;
  final String transcript;
  final List<String> tools;
  final List<String> tags;
  final String category;
  final String caption;
  final List<String> hashtags;
  final List<String> urls;
  final String author;
  final String? date;
  final String analysis; // full / audio-only / caption-only
  final String source; // webview / gallery / shared-file
  final String model;

  Map<String, dynamic> toJson() => {
        'shortcode': shortcode,
        'link': link,
        'title': title,
        'summary': summary,
        'key_ideas': keyIdeas,
        'on_screen_text': onScreenText,
        'transcript': transcript,
        'tools': tools,
        'tags': tags,
        'category': category,
        'caption': caption,
        'hashtags': hashtags,
        'urls': urls,
        'author': author,
        'date': date,
        'analysis': analysis,
        'source': source,
        'model': model,
      };

  factory Reel.fromJson(Map<String, dynamic> j) {
    List<String> list(String k) => [for (final x in (j[k] as List? ?? const [])) x.toString()];
    String str(String k) => (j[k] ?? '').toString();
    return Reel(
      shortcode: str('shortcode'),
      link: str('link'),
      title: str('title'),
      summary: str('summary'),
      keyIdeas: list('key_ideas'),
      onScreenText: str('on_screen_text'),
      transcript: str('transcript'),
      tools: list('tools'),
      tags: list('tags'),
      category: str('category'),
      caption: str('caption'),
      hashtags: list('hashtags'),
      urls: list('urls'),
      author: str('author'),
      date: j['date'] as String?,
      analysis: str('analysis').isEmpty ? 'full' : str('analysis'),
      source: str('source'),
      model: str('model'),
    );
  }

  /// Text indexed for library search.
  String get searchText => [title, summary, ...keyIdeas, ...tools, ...tags, ...hashtags, category, author].join(' ');
}

enum JobStatus { queued, running, done, failed }

/// One reel waiting for / going through the on-device pipeline.
class Job {
  Job({
    required this.id,
    required this.shortcode,
    this.url,
    this.filePath,
    this.status = JobStatus.queued,
    this.step = '',
    this.error,
    this.attempts = 0,
  });

  final int id;
  final String shortcode;
  final String? url; // Instagram link (null for a video picked from the gallery)
  final String? filePath; // local video the user picked/shared, if any
  final JobStatus status;
  final String step; // Fetching… / Analyzing… shown in the UI
  final String? error;
  final int attempts;
}
