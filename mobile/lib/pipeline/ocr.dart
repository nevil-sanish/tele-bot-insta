import 'dart:io';

import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_thumbnail/video_thumbnail.dart';

import 'analyzer.dart';

/// On-device OCR (Google ML Kit, free, no network): grab a frame every few
/// seconds of the first 90 s and read its text. Used only in the backup path;
/// Gemini reads on-screen text itself.
class MlKitScreenTextReader implements OnScreenTextReader {
  MlKitScreenTextReader({this.everySeconds = 3, this.maxSeconds = 90});

  final int everySeconds;
  final int maxSeconds;

  @override
  Future<String> read(File video) async {
    final recognizer = TextRecognizer(script: TextRecognitionScript.latin);
    final dir = await getTemporaryDirectory();
    final seen = <String>{};
    final lines = <String>[];
    try {
      for (var s = 0; s <= maxSeconds; s += everySeconds) {
        final frame = await VideoThumbnail.thumbnailFile(
          video: video.path,
          thumbnailPath: dir.path,
          imageFormat: ImageFormat.JPEG,
          timeMs: s * 1000,
          quality: 75,
        );
        if (frame == null) break; // past the end of the video
        try {
          final result = await recognizer.processImage(InputImage.fromFilePath(frame));
          for (final block in result.blocks) {
            final text = block.text.replaceAll('\n', ' ').trim();
            if (text.length > 2 && seen.add(text.toLowerCase())) lines.add(text);
          }
        } finally {
          await File(frame).delete().catchError((_) => File(frame));
        }
      }
    } finally {
      await recognizer.close();
    }
    return lines.join('\n');
  }
}
