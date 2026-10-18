import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app_services.dart';
import '../core/models.dart';
import 'chat_screen.dart';

class ReelScreen extends StatelessWidget {
  const ReelScreen({super.key, required this.reel});
  final Reel reel;

  Future<void> _report(BuildContext context) async {
    final reason = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Report a wrong summary'),
        content: TextField(
          controller: reason,
          maxLines: 3,
          decoration: const InputDecoration(hintText: "What's wrong? (optional)", border: OutlineInputBorder()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Report')),
        ],
      ),
    );
    if (ok != true || !context.mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    try {
      await AppServices.instance.api.report(reel.shortcode, reason.text);
      messenger.showSnackBar(const SnackBar(content: Text('Thanks — reports help us re-check summaries.')));
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('$e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    Widget section(String title, Widget child) => Padding(
          padding: const EdgeInsets.only(top: 20),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: theme.textTheme.titleSmall?.copyWith(color: theme.colorScheme.primary)),
            const SizedBox(height: 6),
            child,
          ]),
        );
    Widget expandable(String title, String text) => ExpansionTile(
          tilePadding: EdgeInsets.zero,
          title: Text(title, style: theme.textTheme.titleSmall),
          children: [Align(alignment: Alignment.centerLeft, child: SelectableText(text))],
        );

    return Scaffold(
      appBar: AppBar(
        actions: [
          if (reel.link.isNotEmpty)
            IconButton(
              tooltip: 'Open in Instagram',
              icon: const Icon(Icons.open_in_new),
              onPressed: () => launchUrl(Uri.parse(reel.link), mode: LaunchMode.externalApplication),
            ),
          PopupMenuButton<String>(
            onSelected: (v) {
              if (v == 'copy') {
                Clipboard.setData(ClipboardData(text: '${reel.title}\n\n${reel.summary}\n${reel.link}'));
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Copied')));
              } else if (v == 'report') {
                _report(context);
              }
            },
            itemBuilder: (_) => [
              const PopupMenuItem(value: 'copy', child: Text('Copy summary')),
              if (!reel.shortcode.startsWith('app_'))
                const PopupMenuItem(value: 'report', child: Text('Report wrong summary')),
            ],
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => Navigator.push(context, MaterialPageRoute(builder: (_) => ChatScreen(reel: reel))),
        icon: const Icon(Icons.chat_bubble_outline),
        label: const Text('Ask about it'),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 96),
        children: [
          SelectableText(reel.title, style: theme.textTheme.headlineSmall),
          if (reel.author.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text('@${reel.author}', style: theme.textTheme.labelLarge),
            ),
          if (reel.summary.isNotEmpty) section('Summary', SelectableText(reel.summary, style: theme.textTheme.bodyLarge)),
          if (reel.keyIdeas.isNotEmpty)
            section(
              'Key ideas',
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                for (final k in reel.keyIdeas)
                  Padding(padding: const EdgeInsets.only(bottom: 6), child: SelectableText('•  $k')),
              ]),
            ),
          if (reel.tools.isNotEmpty)
            section('Tools & things mentioned',
                Wrap(spacing: 6, runSpacing: 6, children: [for (final t in reel.tools) Chip(label: Text(t))])),
          if (reel.urls.isNotEmpty)
            section(
              'Links',
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                for (final u in reel.urls)
                  InkWell(
                    onTap: () => launchUrl(Uri.parse(u), mode: LaunchMode.externalApplication),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      child: Text(u, style: TextStyle(color: theme.colorScheme.primary)),
                    ),
                  ),
              ]),
            ),
          if (reel.tags.isNotEmpty)
            section('Tags', Wrap(spacing: 6, runSpacing: 6, children: [for (final t in reel.tags) Chip(label: Text('#$t'))])),
          const SizedBox(height: 12),
          if (reel.onScreenText.isNotEmpty) expandable('On-screen text', reel.onScreenText),
          if (reel.transcript.isNotEmpty) expandable('Transcript', reel.transcript),
          if (reel.caption.isNotEmpty) expandable('Original caption', reel.caption),
          const SizedBox(height: 12),
          Text(
            [
              if (reel.category.isNotEmpty) reel.category,
              if (reel.analysis != 'full') 'analysis: ${reel.analysis}',
              if (reel.model.isNotEmpty) reel.model,
            ].join(' · '),
            style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.outline),
          ),
        ],
      ),
    );
  }
}
