/// Errors from the on-device pipeline, with a message fit to show the user.
class PipelineException implements Exception {
  PipelineException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// An HTTP error from an AI provider, classified the way the bot classifies them.
class ApiException implements Exception {
  ApiException(this.provider, this.status, this.body);
  final String provider;
  final int status;
  final String body;

  bool get isRateLimit => status == 429 || body.contains('RESOURCE_EXHAUSTED');
  bool get isBadRequest => status == 400;
  bool get isAuth => status == 401 || status == 403;

  @override
  String toString() {
    final short = body.length > 200 ? '${body.substring(0, 200)}…' : body;
    return '$provider HTTP $status: $short';
  }
}
