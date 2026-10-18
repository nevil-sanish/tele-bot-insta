import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_services.dart';
import '../core/models.dart';
import '../core/text_utils.dart';
import 'account_screen.dart';
import 'chat_screen.dart';
import 'reel_screen.dart';
import 'settings_screen.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final _search = TextEditingController();
  StreamSubscription<void>? _changes;
  bool _ownKey = false;

  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    _changes = _s.runner.changes.listen((_) {
      if (mounted) setState(() {});
    });
    _s.session.addListener(_refreshUi);
    _checkKey();
    _sync(silent: true);
  }

  @override
  void dispose() {
    _changes?.cancel();
    _s.session.removeListener(_refreshUi);
    _search.dispose();
    super.dispose();
  }

  void _refreshUi() {
    if (mounted) setState(() {});
  }

  Future<void> _checkKey() async {
    final has = await _s.hasOwnKey();
    if (mounted) setState(() => _ownKey = has);
  }

  Future<void> _sync({bool silent = false}) async {
    try {
      await _s.runner.sync();
      await _s.session.refresh();
    } catch (e) {
      if (!silent && mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
    }
  }

  Future<void> _add() async {
    final clip = await Clipboard.getData(Clipboard.kTextPlain);
    if (!mounted) return;
    final result = await showModalBottomSheet<_AddChoice>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _AddSheet(initial: findIgUrl(clip?.text) ?? ''),
    );
    if (result == null || !mounted) return;
    Reel? saved;
    if (result.url != null) {
      saved = _s.runner.submit(url: result.url);
    } else if (result.file != null) {
      saved = _s.runner.submit(filePath: result.file!.path, fileKey: await AppServices.fileKey(result.file!));
    }
    if (!mounted) return;
    if (saved != null) {
      Navigator.push(context, MaterialPageRoute(builder: (_) => ReelScreen(reel: saved!)));
    }
  }

  Future<void> _openSettings() async {
    await Navigator.push(context, MaterialPageRoute(builder: (_) => const SettingsScreen()));
    _checkKey();
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim();
    final reels = query.isEmpty ? _s.db.recent(limit: 200) : _s.db.search(query, limit: 50);
    final jobs = _s.db.activeJobs();
    final account = _s.session.account;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Reel Brain'),
        actions: [
          IconButton(
            tooltip: 'Ask your library',
            icon: const Icon(Icons.forum_outlined),
            onPressed: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const ChatScreen())),
          ),
          IconButton(
            tooltip: 'Account',
            icon: const Icon(Icons.account_circle_outlined),
            onPressed: () async {
              await Navigator.push(context, MaterialPageRoute(builder: (_) => const AccountScreen()));
              _checkKey();
            },
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _add,
        icon: const Icon(Icons.add),
        label: const Text('Add reel'),
      ),
      body: RefreshIndicator(
        onRefresh: _sync,
        child: CustomScrollView(
          slivers: [
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
                child: SearchBar(
                  controller: _search,
                  hintText: 'Search your reels',
                  leading: const Icon(Icons.search),
                  trailing: [
                    if (query.isNotEmpty)
                      IconButton(icon: const Icon(Icons.close), onPressed: () => setState(_search.clear)),
                  ],
                  onChanged: (_) => setState(() {}),
                ),
              ),
            ),
            if (account != null)
              SliverToBoxAdapter(child: _ModeBanner(ownKey: _ownKey, account: account, onSetKey: _openSettings)),
            if (jobs.isNotEmpty && query.isEmpty)
              SliverList.list(children: [for (final j in jobs) _JobTile(job: j)]),
            if (reels.isEmpty)
              SliverFillRemaining(
                hasScrollBody: false,
                child: _Empty(searching: query.isNotEmpty),
              )
            else
              SliverList.builder(
                itemCount: reels.length,
                itemBuilder: (_, i) => _ReelTile(reel: reels[i]),
              ),
            const SliverToBoxAdapter(child: SizedBox(height: 96)),
          ],
        ),
      ),
    );
  }
}

class _ModeBanner extends StatelessWidget {
  const _ModeBanner({required this.ownKey, required this.account, required this.onSetKey});
  final bool ownKey;
  final Account account;
  final VoidCallback onSetKey;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final String text;
    if (ownKey) {
      text = 'Using your own AI key — free.';
    } else if (account.unlimited) {
      text = 'Unlimited plan.';
    } else {
      text = '${account.reelsLeft ?? 0} reels left on your ${account.plan} plan.';
    }
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Card(
        color: scheme.secondaryContainer,
        margin: EdgeInsets.zero,
        child: ListTile(
          dense: true,
          leading: Icon(ownKey ? Icons.key : Icons.toll_outlined, color: scheme.onSecondaryContainer),
          title: Text(text, style: TextStyle(color: scheme.onSecondaryContainer)),
          trailing: ownKey || account.unlimited ? null : TextButton(onPressed: onSetKey, child: const Text('Use my key')),
        ),
      ),
    );
  }
}

class _JobTile extends StatelessWidget {
  const _JobTile({required this.job});
  final Job job;

  @override
  Widget build(BuildContext context) {
    final s = AppServices.instance;
    final failed = job.status == JobStatus.failed;
    final scheme = Theme.of(context).colorScheme;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      color: failed ? scheme.errorContainer : null,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 8, 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              if (!failed) const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2)),
              if (failed) Icon(Icons.error_outline, size: 18, color: scheme.onErrorContainer),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  failed ? 'Failed' : (job.step.isEmpty ? 'Waiting…' : job.step),
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
            ]),
            const SizedBox(height: 4),
            Text(job.url ?? 'Video from your phone', maxLines: 1, overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall),
            if (failed && job.error != null) ...[
              const SizedBox(height: 6),
              Text(job.error!, style: TextStyle(color: scheme.onErrorContainer)),
            ],
            if (failed)
              Wrap(spacing: 4, children: [
                TextButton(
                  onPressed: () {
                    s.db.retryJob(job.id);
                    s.runner.notify();
                    s.runner.drain();
                  },
                  child: const Text('Retry'),
                ),
                TextButton(
                  onPressed: () async {
                    final picked = await FilePicker.pickFiles(type: FileType.video);
                    final path = picked.isEmpty ? null : picked.first.path;
                    if (path == null) return;
                    s.db.retryJob(job.id, filePath: path);
                    s.runner.notify();
                    s.runner.drain();
                  },
                  child: const Text('Pick the video'),
                ),
                TextButton(
                  onPressed: () {
                    s.db.dismissJob(job.id);
                    s.runner.notify();
                  },
                  child: const Text('Dismiss'),
                ),
              ]),
          ],
        ),
      ),
    );
  }
}

class _ReelTile extends StatelessWidget {
  const _ReelTile({required this.reel});
  final Reel reel;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      title: Text(reel.title, maxLines: 2, overflow: TextOverflow.ellipsis),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (reel.author.isNotEmpty) Text('@${reel.author}', style: theme.textTheme.labelMedium),
          if (reel.summary.isNotEmpty) Text(reel.summary, maxLines: 2, overflow: TextOverflow.ellipsis),
          if (reel.tags.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(reel.tags.take(4).map((t) => '#$t').join('  '),
                  style: theme.textTheme.labelSmall?.copyWith(color: theme.colorScheme.primary)),
            ),
        ],
      ),
      onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => ReelScreen(reel: reel))),
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.searching});
  final bool searching;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(searching ? Icons.search_off : Icons.video_library_outlined, size: 56, color: theme.colorScheme.outline),
          const SizedBox(height: 12),
          Text(
            searching
                ? 'No saved reels match that.'
                : 'Your library is empty.\n\nIn Instagram, tap Share on any reel → Reel Brain. '
                    'Or tap "Add reel" to paste a link.',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodyLarge,
          ),
        ],
      ),
    );
  }
}

class _AddChoice {
  _AddChoice({this.url, this.file});
  final String? url;
  final File? file;
}

class _AddSheet extends StatefulWidget {
  const _AddSheet({required this.initial});
  final String initial;

  @override
  State<_AddSheet> createState() => _AddSheetState();
}

class _AddSheetState extends State<_AddSheet> {
  late final _link = TextEditingController(text: widget.initial);
  String? _error;

  @override
  void dispose() {
    _link.dispose();
    super.dispose();
  }

  void _submit() {
    final url = findIgUrl(_link.text);
    if (url == null) {
      setState(() => _error = 'Paste an Instagram reel or post link.');
      return;
    }
    Navigator.pop(context, _AddChoice(url: url));
  }

  Future<void> _pick() async {
    final picked = await FilePicker.pickFiles(type: FileType.video);
    final path = picked.isEmpty ? null : picked.first.path;
    if (path != null && mounted) Navigator.pop(context, _AddChoice(file: File(path)));
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.viewInsetsOf(context).bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Add a reel', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          TextField(
            controller: _link,
            autofocus: widget.initial.isEmpty,
            keyboardType: TextInputType.url,
            decoration: InputDecoration(
              labelText: 'Instagram link',
              hintText: 'https://www.instagram.com/reel/…',
              errorText: _error,
              border: const OutlineInputBorder(),
            ),
            onSubmitted: (_) => _submit(),
          ),
          const SizedBox(height: 12),
          FilledButton(onPressed: _submit, child: const Text('Analyze')),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: _pick,
            icon: const Icon(Icons.video_file_outlined),
            label: const Text('Pick a video from this phone'),
          ),
        ],
      ),
    );
  }
}
