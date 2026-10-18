import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:receive_sharing_intent/receive_sharing_intent.dart';

import 'app_services.dart';
import 'core/text_utils.dart';
import 'ui/home_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final services = await AppServices.init();
  unawaited(services.runner.resume()); // finish anything interrupted last time
  runApp(const ReelBrainApp());
}

class ReelBrainApp extends StatefulWidget {
  const ReelBrainApp({super.key});

  @override
  State<ReelBrainApp> createState() => _ReelBrainAppState();
}

class _ReelBrainAppState extends State<ReelBrainApp> {
  final _messenger = GlobalKey<ScaffoldMessengerState>();
  StreamSubscription<List<SharedMediaFile>>? _shares;

  @override
  void initState() {
    super.initState();
    // Shared from Instagram (or any app) while running, or to start the app.
    _shares = ReceiveSharingIntent.instance.getMediaStream().listen(_onShared);
    ReceiveSharingIntent.instance.getInitialMedia().then((items) {
      _onShared(items);
      ReceiveSharingIntent.instance.reset();
    });
  }

  @override
  void dispose() {
    _shares?.cancel();
    super.dispose();
  }

  Future<void> _onShared(List<SharedMediaFile> items) async {
    final services = AppServices.instance;
    for (final item in items) {
      if (item.type == SharedMediaType.text || item.type == SharedMediaType.url) {
        final url = findIgUrl(item.path);
        if (url == null) {
          _toast("That isn't an Instagram reel or post link.");
          continue;
        }
        final saved = services.runner.submit(url: url);
        _toast(saved != null ? 'Already in your library: ${saved.title}' : 'Saved. Analyzing on your phone…');
      } else if (item.type == SharedMediaType.video || item.type == SharedMediaType.image) {
        final file = File(item.path);
        final saved = services.runner.submit(filePath: file.path, fileKey: await AppServices.fileKey(file));
        _toast(saved != null ? 'Already in your library: ${saved.title}' : 'Saved. Analyzing on your phone…');
      }
    }
  }

  void _toast(String text) => _messenger.currentState?.showSnackBar(SnackBar(content: Text(text)));

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Reel Brain',
      scaffoldMessengerKey: _messenger,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF6750A4), useMaterial3: true),
      darkTheme: ThemeData(colorSchemeSeed: const Color(0xFF6750A4), brightness: Brightness.dark, useMaterial3: true),
      home: const HomeScreen(),
    );
  }
}
