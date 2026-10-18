import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app_services.dart';
import '../core/config.dart';

/// Join this account with the Instagram bot: DM "link 123456" to the bot.
class LinkScreen extends StatefulWidget {
  const LinkScreen({super.key});

  @override
  State<LinkScreen> createState() => _LinkScreenState();
}

class _LinkScreenState extends State<LinkScreen> with WidgetsBindingObserver {
  String? _code;
  int _left = 0;
  String? _error;
  Timer? _timer;

  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _newCode();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  /// Coming back from Instagram: check whether the link went through.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _check();
  }

  Future<void> _newCode() async {
    try {
      final (code, seconds) = await _s.api.linkCode();
      _timer?.cancel();
      setState(() {
        _code = code;
        _left = seconds;
        _error = null;
      });
      _timer = Timer.periodic(const Duration(seconds: 1), (t) {
        if (!mounted) return;
        setState(() => _left = _left > 0 ? _left - 1 : 0);
        if (_left == 0) t.cancel();
      });
    } catch (e) {
      setState(() => _error = '$e');
    }
  }

  Future<void> _check() async {
    try {
      await _s.session.refresh();
      if (_s.session.account?.instagramLinked == true && mounted) {
        await _s.runner.sync();
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Instagram linked ✅')));
        Navigator.pop(context);
      }
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final message = 'link ${_code ?? ''}';
    return Scaffold(
      appBar: AppBar(title: const Text('Link Instagram')),
      body: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(
            'Send this message to @${AppConfig.botUsername} on Instagram. After that, reels you DM to the bot '
            'appear in this app, and you have one shared balance.',
            style: theme.textTheme.bodyLarge,
          ),
          const SizedBox(height: 24),
          if (_error != null) Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
          if (_code != null) ...[
            Center(child: SelectableText(message, style: theme.textTheme.displaySmall)),
            const SizedBox(height: 8),
            Center(
              child: Text(_left > 0 ? 'Expires in ${_left ~/ 60}:${(_left % 60).toString().padLeft(2, '0')}' : 'Expired',
                  style: theme.textTheme.bodySmall),
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: _left == 0
                  ? null
                  : () async {
                      await Clipboard.setData(ClipboardData(text: message));
                      await launchUrl(Uri.parse('https://ig.me/m/${AppConfig.botUsername}'),
                          mode: LaunchMode.externalApplication);
                    },
              icon: const Icon(Icons.send),
              label: const Text('Copy & open Instagram'),
            ),
            const SizedBox(height: 8),
            OutlinedButton(onPressed: _check, child: const Text("I've sent it")),
            TextButton(onPressed: _newCode, child: const Text('New code')),
          ],
        ],
      ),
    );
  }
}
