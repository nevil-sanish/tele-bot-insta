import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

import 'errors.dart';

/// A reel's media downloaded to the phone, plus what the page told us about it.
class FetchedReel {
  FetchedReel({required this.file, required this.mimeType, this.caption, this.author, this.source = 'phone'});
  final File file;
  final String mimeType;
  final String? caption;
  final String? author;
  final String source;
}

abstract class ReelFetcher {
  Future<FetchedReel> fetch(String url, String shortcode);
}

/// What the extraction script found on an Instagram page.
class PageExtraction {
  PageExtraction({this.video = '', this.image = '', this.caption = '', this.author = ''});
  final String video;
  final String image;
  final String caption;
  final String author;

  bool get hasMedia => video.isNotEmpty || image.isNotEmpty;
}

/// Runs inside the page: the playable video URL (or the post image), caption and author.
const extractionScript = r'''
(() => {
  const q = (s) => document.querySelector(s);
  const v = q('video');
  let video = v ? (v.currentSrc || v.src || ((v.querySelector('source') || {}).src) || '') : '';
  if (video.startsWith('blob:')) video = '';
  if (!video) {
    const m = document.documentElement.innerHTML.match(/"video_url":"([^"]+)"/);
    if (m) { try { video = JSON.parse('"' + m[1] + '"'); } catch (e) {} }
  }
  if (!video) video = (q('meta[property="og:video"]') || {}).content || '';
  const image = (q('img.EmbeddedMediaImage') || q('meta[property="og:image"]') || {}).src
             || (q('meta[property="og:image"]') || {}).content || '';
  const caption = ((q('.Caption') || {}).innerText || (q('meta[property="og:description"]') || {}).content || '');
  const author = ((q('.UsernameText') || q('.Username') || {}).innerText || '');
  return JSON.stringify({video, image, caption, author});
})()
''';

/// Parse the script's result and tidy the caption (the embed page prefixes the
/// username and appends "View all N comments").
PageExtraction parseExtraction(Object? result) {
  if (result is! String || result.isEmpty) return PageExtraction();
  Map<String, dynamic> j;
  try {
    j = jsonDecode(result) as Map<String, dynamic>;
  } catch (_) {
    return PageExtraction();
  }
  final author = (j['author'] as String? ?? '').trim();
  var caption = (j['caption'] as String? ?? '').trim();
  if (author.isNotEmpty && caption.startsWith(author)) caption = caption.substring(author.length).trim();
  caption = caption.replaceFirst(RegExp(r'\s*View all [\d,.]+ comments?\s*$', caseSensitive: false), '').trim();
  return PageExtraction(
    video: (j['video'] as String? ?? '').trim(),
    image: (j['image'] as String? ?? '').trim(),
    caption: caption,
    author: author,
  );
}

/// Public pages to try, in order: the embed page, then the full post page.
/// Always logged out: the app never signs in to Instagram.
List<String> candidatePages(String shortcode) => [
      'https://www.instagram.com/reel/$shortcode/embed/captioned/',
      'https://www.instagram.com/p/$shortcode/embed/captioned/',
      'https://www.instagram.com/reel/$shortcode/',
    ];

/// Fetches on the phone itself: a hidden WebView (the phone's real browser
/// engine, on the phone's own network) opens the page, the script reads the
/// media URL, and the file is downloaded to the app's cache.
class WebViewFetcher implements ReelFetcher {
  WebViewFetcher({this.pageTimeout = const Duration(seconds: 20), this.maxBytes = 100 * 1024 * 1024});

  final Duration pageTimeout;
  final int maxBytes;

  @override
  Future<FetchedReel> fetch(String url, String shortcode) async {
    PageExtraction found = PageExtraction();
    for (final page in candidatePages(shortcode)) {
      found = await _extract(page);
      if (found.hasMedia) break;
    }
    if (!found.hasMedia) {
      throw PipelineException("Couldn't open this reel from your phone (it may be private or need a login). "
          'Pick the video from your gallery instead.');
    }
    final isVideo = found.video.isNotEmpty;
    final file = await _download(isVideo ? found.video : found.image, isVideo ? '.mp4' : '.jpg');
    return FetchedReel(
      file: file,
      mimeType: isVideo ? 'video/mp4' : 'image/jpeg',
      caption: found.caption,
      author: found.author,
    );
  }

  Future<PageExtraction> _extract(String page) async {
    final done = Completer<PageExtraction>();
    late HeadlessInAppWebView view;
    view = HeadlessInAppWebView(
      initialUrlRequest: URLRequest(url: WebUri(page)),
      initialSettings: InAppWebViewSettings(javaScriptEnabled: true, mediaPlaybackRequiresUserGesture: false),
      onLoadStop: (controller, _) async {
        // Media elements appear after the page's scripts run: poll briefly.
        for (var i = 0; i < 16 && !done.isCompleted; i++) {
          final found = parseExtraction(await controller.evaluateJavascript(source: extractionScript));
          if (found.video.isNotEmpty || (i >= 6 && found.hasMedia)) {
            if (!done.isCompleted) done.complete(found);
            return;
          }
          await Future<void>.delayed(const Duration(milliseconds: 500));
        }
        if (!done.isCompleted) done.complete(PageExtraction());
      },
      onReceivedError: (_, _, _) {
        if (!done.isCompleted) done.complete(PageExtraction());
      },
    );
    await view.run();
    try {
      return await done.future.timeout(pageTimeout, onTimeout: PageExtraction.new);
    } finally {
      await view.dispose();
    }
  }

  Future<File> _download(String url, String ext) async {
    final dir = await getTemporaryDirectory();
    final file = File('${dir.path}/reel_${DateTime.now().microsecondsSinceEpoch}$ext');
    final client = http.Client();
    try {
      final resp = await client.send(http.Request('GET', Uri.parse(url))).timeout(const Duration(seconds: 30));
      if (resp.statusCode != 200) throw PipelineException('Download failed (HTTP ${resp.statusCode})');
      final sink = file.openWrite();
      var size = 0;
      await for (final chunk in resp.stream) {
        size += chunk.length;
        if (size > maxBytes) {
          await sink.close();
          await file.delete();
          throw PipelineException('Video is larger than ${maxBytes ~/ (1024 * 1024)} MB');
        }
        sink.add(chunk);
      }
      await sink.close();
      return file;
    } finally {
      client.close();
    }
  }
}
