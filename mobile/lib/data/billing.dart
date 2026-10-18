import 'dart:async';

import 'package:in_app_purchase/in_app_purchase.dart';

import 'api_client.dart';
import 'session.dart';

/// Google Play subscriptions. Play takes the payment; our server verifies the
/// purchase token with Google and adds the plan's credits (once per billing cycle).
class PlayBilling {
  PlayBilling(this.api, this.session, [InAppPurchase? iap]) : _iap = iap ?? InAppPurchase.instance;

  final ApiClient api;
  final Session session;
  final InAppPurchase _iap;
  StreamSubscription<List<PurchaseDetails>>? _sub;

  /// Latest problem to show on the Plans screen (null = none).
  final errors = StreamController<String>.broadcast();

  void start() {
    _sub ??= _iap.purchaseStream.listen(_onPurchases, onError: (Object e) => errors.add('$e'));
  }

  Future<bool> available() => _iap.isAvailable();

  Future<Map<String, ProductDetails>> products(Iterable<String> ids) async {
    final resp = await _iap.queryProductDetails(ids.toSet());
    return {for (final p in resp.productDetails) p.id: p};
  }

  /// Start a purchase; the result arrives on the purchase stream.
  Future<void> buy(ProductDetails product) async {
    final accountId = session.account?.id;
    // applicationUserName becomes Play's obfuscated account ID; the server checks it matches.
    await _iap.buyNonConsumable(
      purchaseParam: PurchaseParam(productDetails: product, applicationUserName: accountId?.toString()),
    );
  }

  Future<void> restore() => _iap.restorePurchases();

  Future<void> _onPurchases(List<PurchaseDetails> purchases) async {
    for (final p in purchases) {
      if (p.status == PurchaseStatus.error) {
        errors.add(p.error?.message ?? 'The purchase failed.');
      } else if (p.status == PurchaseStatus.purchased || p.status == PurchaseStatus.restored) {
        try {
          await api.playPurchase(p.verificationData.serverVerificationData);
          await session.refresh();
        } on ApiError catch (e) {
          errors.add(e.message);
          continue; // not completed: Play redelivers it and we retry
        }
      }
      if (p.pendingCompletePurchase) await _iap.completePurchase(p);
    }
  }

  void dispose() {
    _sub?.cancel();
    errors.close();
  }
}
