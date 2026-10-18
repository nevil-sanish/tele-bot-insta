import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app_services.dart';
import '../core/config.dart';
import 'link_screen.dart';
import 'plans_screen.dart';
import 'settings_screen.dart';

class AccountScreen extends StatefulWidget {
  const AccountScreen({super.key});

  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    _s.session.addListener(_changed);
    _s.session.refresh().catchError((_) {});
  }

  @override
  void dispose() {
    _s.session.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete your account?'),
        content: const Text(
          'This erases your library, questions, credits and linked logins, on our server and this phone. '
          'A plan bought on Instagram is cancelled; a Google Play plan must be cancelled in Play Store → Subscriptions. '
          "This can't be undone.",
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Theme.of(ctx).colorScheme.error),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    try {
      await _s.signOut(deleteAccount: true);
      if (mounted) Navigator.of(context).popUntil((r) => r.isFirst);
    } catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final account = _s.session.account;
    final theme = Theme.of(context);
    if (account == null) return const Scaffold(body: SizedBox());
    final plan = account.unlimited
        ? 'Unlimited'
        : '${account.plan[0].toUpperCase()}${account.plan.substring(1)} · ${account.reelsLeft ?? 0} reels left'
            '${account.planUntil != null ? ' · until ${account.planUntil!.toLocal().toString().substring(0, 10)}' : ''}';
    return Scaffold(
      appBar: AppBar(title: const Text('Account')),
      body: ListView(
        children: [
          ListTile(
            leading: const CircleAvatar(child: Icon(Icons.person)),
            title: Text(account.name ?? 'Signed in'),
            subtitle: Text(account.email ?? ''),
          ),
          ListTile(
            leading: const Icon(Icons.toll_outlined),
            title: const Text('Plan & credits'),
            subtitle: Text(plan),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const PlansScreen())),
          ),
          ListTile(
            leading: const Icon(Icons.key_outlined),
            title: const Text('Your AI keys'),
            subtitle: const Text('Use the app free with your own Gemini or Groq key'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const SettingsScreen())),
          ),
          ListTile(
            leading: const Icon(Icons.link),
            title: const Text('Link Instagram'),
            subtitle: Text(account.instagramLinked
                ? 'Linked — reels you DM to @${AppConfig.botUsername} show up here'
                : 'DM reels to @${AppConfig.botUsername} and see them here'),
            trailing: account.instagramLinked ? const Icon(Icons.check_circle, color: Colors.green) : const Icon(Icons.chevron_right),
            onTap: account.instagramLinked
                ? null
                : () => Navigator.push(context, MaterialPageRoute(builder: (_) => const LinkScreen())),
          ),
          const Divider(),
          ListTile(
            leading: const Icon(Icons.privacy_tip_outlined),
            title: const Text('Privacy policy'),
            onTap: () => launchUrl(AppConfig.page('/privacy'), mode: LaunchMode.externalApplication),
          ),
          ListTile(
            leading: const Icon(Icons.description_outlined),
            title: const Text('Terms'),
            onTap: () => launchUrl(AppConfig.page('/terms'), mode: LaunchMode.externalApplication),
          ),
          ListTile(
            leading: const Icon(Icons.logout),
            title: const Text('Sign out'),
            onTap: () async {
              await _s.signOut();
              if (context.mounted) Navigator.of(context).popUntil((r) => r.isFirst);
            },
          ),
          ListTile(
            leading: Icon(Icons.delete_forever_outlined, color: theme.colorScheme.error),
            title: Text('Delete account', style: TextStyle(color: theme.colorScheme.error)),
            onTap: _delete,
          ),
        ],
      ),
    );
  }
}
