import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app_services.dart';
import '../data/key_store.dart';

/// Your own AI keys: with one, the app analyzes reels on your phone for free.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _gemini = TextEditingController();
  final _groq = TextEditingController();
  bool _serverFetch = true;
  bool _loaded = false;

  KeyStore get _keys => AppServices.instance.keys;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    _gemini.text = await _keys.read(KeyStore.gemini) ?? '';
    _groq.text = await _keys.read(KeyStore.groq) ?? '';
    _serverFetch = (await _keys.read(KeyStore.serverFetch)) != '0';
    if (mounted) setState(() => _loaded = true);
  }

  Future<void> _save() async {
    await _keys.write(KeyStore.gemini, _gemini.text);
    await _keys.write(KeyStore.groq, _groq.text);
    await _keys.write(KeyStore.serverFetch, _serverFetch ? '1' : '0');
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Saved')));
    Navigator.pop(context);
  }

  @override
  void dispose() {
    _gemini.dispose();
    _groq.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    Widget link(String text, String url) => TextButton(
          style: TextButton.styleFrom(padding: EdgeInsets.zero, alignment: Alignment.centerLeft),
          onPressed: () => launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication),
          child: Text(text),
        );
    return Scaffold(
      appBar: AppBar(title: const Text('Your AI keys'), actions: [TextButton(onPressed: _save, child: const Text('Save'))]),
      body: !_loaded
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text(
                  'With your own key, reels are fetched and analyzed on this phone — free, no credits. '
                  'Keys stay encrypted on this phone and are only sent to Google or Groq, never to us.',
                  style: theme.textTheme.bodyMedium,
                ),
                const SizedBox(height: 20),
                TextField(
                  controller: _gemini,
                  obscureText: true,
                  decoration: const InputDecoration(
                    labelText: 'Gemini API key (recommended)',
                    helperText: 'Watches the video: best summaries',
                    border: OutlineInputBorder(),
                  ),
                ),
                link('Get a free Gemini key →', 'https://aistudio.google.com/apikey'),
                const SizedBox(height: 12),
                TextField(
                  controller: _groq,
                  obscureText: true,
                  decoration: const InputDecoration(
                    labelText: 'Groq API key (optional)',
                    helperText: 'Backup (speech-to-text) and fast answers',
                    border: OutlineInputBorder(),
                  ),
                ),
                link('Get a free Groq key →', 'https://console.groq.com/keys'),
                const Divider(height: 32),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _serverFetch,
                  onChanged: (v) => setState(() => _serverFetch = v),
                  title: const Text('Server fetch when my phone can\'t open a reel'),
                  subtitle: const Text('Uses a few credits (less than a third of a reel)'),
                ),
                const SizedBox(height: 12),
                Text(
                  'Without a key, the server analyzes reels using your credits.',
                  style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.outline),
                ),
              ],
            ),
    );
  }
}
