import 'package:flutter/material.dart';

import '../app_services.dart';
import '../core/models.dart';

/// Ask about one reel, or (with no reel) about everything in the library.
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, this.reel});
  final Reel? reel;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final List<Map<String, String>> _messages = [];
  bool _busy = false;

  AppServices get _s => AppServices.instance;

  @override
  void initState() {
    super.initState();
    // Own-key chats about a reel keep their recent history on the phone.
    if (widget.reel != null) _messages.addAll(_s.db.chatHistory(widget.reel!.shortcode));
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final q = _input.text.trim();
    if (q.isEmpty || _busy) return;
    _input.clear();
    setState(() {
      _messages.add({'role': 'user', 'content': q});
      _busy = true;
    });
    _toBottom();
    try {
      final answer = await _s.ask(q, reel: widget.reel);
      _messages.add({'role': 'assistant', 'content': answer});
    } catch (e) {
      _messages.add({'role': 'error', 'content': '$e'});
    } finally {
      if (mounted) setState(() => _busy = false);
      _toBottom();
    }
  }

  void _toBottom() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scroll.hasClients) {
          _scroll.animateTo(_scroll.position.maxScrollExtent,
              duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
        }
      });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final reel = widget.reel;
    return Scaffold(
      appBar: AppBar(
        title: Text(reel == null ? 'Ask your library' : 'Ask about this reel', overflow: TextOverflow.ellipsis),
      ),
      body: Column(
        children: [
          Expanded(
            child: _messages.isEmpty
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(32),
                      child: Text(
                        reel == null
                            ? 'Ask anything — I search your saved reels and answer with sources.\n\n'
                                'e.g. "Which reels mentioned a Python library?"'
                            : 'Ask anything about "${reel.title}".\n\ne.g. "What were the steps?"',
                        textAlign: TextAlign.center,
                        style: theme.textTheme.bodyLarge?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                      ),
                    ),
                  )
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.all(12),
                    itemCount: _messages.length + (_busy ? 1 : 0),
                    itemBuilder: (_, i) {
                      if (i == _messages.length) {
                        return const Align(
                          alignment: Alignment.centerLeft,
                          child: Padding(padding: EdgeInsets.all(12), child: Text('…')),
                        );
                      }
                      final m = _messages[i];
                      final mine = m['role'] == 'user';
                      final error = m['role'] == 'error';
                      return Align(
                        alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
                        child: Container(
                          constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.82),
                          margin: const EdgeInsets.symmetric(vertical: 4),
                          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                          decoration: BoxDecoration(
                            color: error
                                ? theme.colorScheme.errorContainer
                                : mine
                                    ? theme.colorScheme.primaryContainer
                                    : theme.colorScheme.surfaceContainerHighest,
                            borderRadius: BorderRadius.circular(16),
                          ),
                          child: SelectableText(m['content']!),
                        ),
                      );
                    },
                  ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
              child: Row(children: [
                Expanded(
                  child: TextField(
                    controller: _input,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _send(),
                    decoration: const InputDecoration(hintText: 'Ask a question', border: OutlineInputBorder()),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(onPressed: _busy ? null : _send, icon: const Icon(Icons.send)),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}
