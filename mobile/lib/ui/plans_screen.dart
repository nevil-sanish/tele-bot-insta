import 'dart:async';

import 'package:flutter/material.dart';
import 'package:in_app_purchase/in_app_purchase.dart';

import '../app_services.dart';
import '../core/models.dart';
import 'settings_screen.dart';

/// Monthly plans through Google Play. (Instagram users can also pay in the DM.)
class PlansScreen extends StatefulWidget {
  const PlansScreen({super.key});

  @override
  State<PlansScreen> createState() => _PlansScreenState();
}

class _PlansScreenState extends State<PlansScreen> {
  List<PlanInfo> _plans = [];
  Map<String, ProductDetails> _products = {};
  bool _playAvailable = false;
  bool _loading = true;
  String? _error;
  StreamSubscription<String>? _errors;

  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    _errors = _s.billing.errors.stream.listen((e) {
      if (mounted) setState(() => _error = e);
    });
    _s.session.addListener(_changed);
    _load();
  }

  @override
  void dispose() {
    _errors?.cancel();
    _s.session.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _load() async {
    try {
      _plans = await _s.api.plans();
      _playAvailable = await _s.billing.available();
      if (_playAvailable) _products = await _s.billing.products(_plans.map((p) => p.productId));
    } catch (e) {
      _error = '$e';
    }
    if (mounted) setState(() => _loading = false);
  }

  @override
  Widget build(BuildContext context) {
    final account = _s.session.account;
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Plans & credits')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (account != null)
                  Card(
                    child: ListTile(
                      title: Text(account.unlimited ? 'Unlimited plan' : '${account.reelsLeft ?? 0} reels left'),
                      subtitle: Text(account.unlimited ? '' : 'Plan: ${account.plan} · ${account.units} credits'),
                    ),
                  ),
                const SizedBox(height: 8),
                for (final plan in _plans) _PlanCard(plan: plan, product: _products[plan.productId]),
                if (!_playAvailable)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 8),
                    child: Text('Google Play billing is not available on this device.'),
                  ),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
                  ),
                TextButton(onPressed: _playAvailable ? _s.billing.restore : null, child: const Text('Restore purchases')),
                const Divider(height: 32),
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.key_outlined),
                  title: const Text('Prefer free? Use your own AI key'),
                  subtitle: const Text('Reels are analyzed on your phone with your Gemini/Groq key — no credits needed.'),
                  onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const SettingsScreen())),
                ),
                Text(
                  'Plans renew monthly; cancel any time in Play Store → Subscriptions. Unused credits don’t carry over.',
                  style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.outline),
                ),
              ],
            ),
    );
  }
}

class _PlanCard extends StatelessWidget {
  const _PlanCard({required this.plan, required this.product});
  final PlanInfo plan;
  final ProductDetails? product;

  @override
  Widget build(BuildContext context) {
    final current = AppServices.instance.session.account?.plan == plan.key;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(plan.title, style: Theme.of(context).textTheme.titleLarge),
                Text('${plan.reels} reels a month'),
                Text(product?.price != null ? '${product!.price} / month' : '₹${plan.priceInr} / month',
                    style: Theme.of(context).textTheme.titleMedium),
              ]),
            ),
            current
                ? const Chip(label: Text('Current'))
                : FilledButton(
                    onPressed: product == null ? null : () => AppServices.instance.billing.buy(product!),
                    child: const Text('Subscribe'),
                  ),
          ],
        ),
      ),
    );
  }
}
