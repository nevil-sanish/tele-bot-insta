import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:receive_sharing_intent/receive_sharing_intent.dart';

import 'app_services.dart';
import 'core/text_utils.dart';
import 'ui/home_screen.dart';
import 'ui/sign_in_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final services = await AppServices.init();
  await services.session.restore();
  services.billing.start();
  if (services.session.signedIn) unawaited(services.runner.resume());
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
  final _waiting = <SharedMediaFile>[]; // shared before signing in

  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    // Shared from Instagram ("Share → Reel Brain") while running, or to start the app.
    _shares = ReceiveSharingIntent.instance.getMediaStream().listen(_onShared);
    ReceiveSharingIntent.instance.getInitialMedia().then((items) {
      _onShared(items);
      ReceiveSharingIntent.instance.reset();
    });
    _s.session.addListener(_onSession);
  }

  @override
  void dispose() {
    _shares?.cancel();
    _s.session.removeListener(_onSession);
    super.dispose();
  }

  void _onSession() {
    setState(() {});
    if (_s.session.signedIn && _waiting.isNotEmpty) {
      final items = List.of(_waiting);
      _waiting.clear();
      _onShared(items);
    }
  }

  Future<void> _onShared(List<SharedMediaFile> items) async {
    if (items.isEmpty) return;
    if (!_s.session.signedIn) {
      _waiting.addAll(items);
      _toast('Sign in first — your reel will be added right after.');
      return;
    }
    for (final item in items) {
      if (item.type == SharedMediaType.text || item.type == SharedMediaType.url) {
        final url = findIgUrl(item.path) ?? findIgUrl(item.message);
        if (url == null) {
          _toast("That isn't an Instagram reel or post link.");
          continue;
        }
        final saved = _s.runner.submit(url: url);
        _toast(saved != null ? 'Already in your library: ${saved.title}' : 'Added. Analyzing…');
      } else if (item.type == SharedMediaType.video || item.type == SharedMediaType.image) {
        final file = File(item.path);
        final saved = _s.runner.submit(filePath: file.path, fileKey: await AppServices.fileKey(file));
        _toast(saved != null ? 'Already in your library: ${saved.title}' : 'Added. Analyzing…');
      }
    }
  }

  void _toast(String text) => _messenger.currentState?.showSnackBar(SnackBar(content: Text(text)));

  @override
  Widget build(BuildContext context) {
    final session = _s.session;
    return MaterialApp(
      title: 'Reel Brain',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: _messenger,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF6750A4), useMaterial3: true),
      darkTheme: ThemeData(colorSchemeSeed: const Color(0xFF6750A4), brightness: Brightness.dark, useMaterial3: true),
      home: session.restoring
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : session.signedIn
              ? const HomeScreen()
              : const SignInScreen(),
    );
  }
}
