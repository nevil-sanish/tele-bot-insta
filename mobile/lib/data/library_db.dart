import 'dart:convert';

import 'package:sqlite3/sqlite3.dart';

import '../core/models.dart';

/// Everything the app keeps on the phone: a copy of the library (with full-text
/// search, works offline), the job queue (survives the app being closed) and
/// chat history. The server's library is the source of truth; see sync().
class LibraryDb {
  LibraryDb(this.db) {
    db.execute('''
      PRAGMA journal_mode = WAL;
      CREATE TABLE IF NOT EXISTS reels (
        shortcode TEXT PRIMARY KEY,
        data      TEXT NOT NULL,
        saved_at  INTEGER NOT NULL
      );
      -- porter = stemming, so "comprehension" also finds "comprehensions".
      CREATE VIRTUAL TABLE IF NOT EXISTS reels_fts USING fts5(
        shortcode UNINDEXED, body, tokenize = 'porter unicode61'
      );
      CREATE TABLE IF NOT EXISTS jobs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        shortcode  TEXT NOT NULL,
        url        TEXT,
        file_path  TEXT,
        status     TEXT NOT NULL DEFAULT 'queued',
        step       TEXT NOT NULL DEFAULT '',
        error      TEXT,
        attempts   INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS chats (
        shortcode TEXT PRIMARY KEY,
        history   TEXT NOT NULL DEFAULT '[]'
      );
    ''');
    _addColumn('jobs', 'server_job_id', 'INTEGER');
    _addColumn('reels', 'pending_upload', 'INTEGER NOT NULL DEFAULT 0');
  }

  void _addColumn(String table, String column, String type) {
    final cols = db.select('PRAGMA table_info($table)').map((r) => r['name']);
    if (!cols.contains(column)) db.execute('ALTER TABLE $table ADD COLUMN $column $type');
  }

  factory LibraryDb.open(String path) => LibraryDb(sqlite3.open(path));
  factory LibraryDb.memory() => LibraryDb(sqlite3.openInMemory());

  final Database db;

  static int _now() => DateTime.now().millisecondsSinceEpoch;

  // ---------------------------------------------------------------- reels

  Reel? getReel(String shortcode) {
    final rows = db.select('SELECT data FROM reels WHERE shortcode = ?', [shortcode]);
    return rows.isEmpty ? null : Reel.fromJson(jsonDecode(rows.first['data'] as String) as Map<String, dynamic>);
  }

  /// Insert or update a reel. [pendingUpload]: made on this phone and not yet shared with the server.
  /// [savedAt] keeps the server's order when syncing (defaults to now).
  void saveReel(Reel reel, {bool pendingUpload = false, int? savedAt}) {
    db.execute('BEGIN');
    try {
      db.execute(
        'INSERT INTO reels (shortcode, data, saved_at, pending_upload) VALUES (?, ?, ?, ?) '
        'ON CONFLICT (shortcode) DO UPDATE SET data = excluded.data, pending_upload = excluded.pending_upload, '
        'saved_at = excluded.saved_at',
        [reel.shortcode, jsonEncode(reel.toJson()), savedAt ?? _now(), pendingUpload ? 1 : 0],
      );
      db.execute('DELETE FROM reels_fts WHERE shortcode = ?', [reel.shortcode]);
      db.execute('INSERT INTO reels_fts (shortcode, body) VALUES (?, ?)', [reel.shortcode, reel.searchText]);
      db.execute('COMMIT');
    } catch (_) {
      db.execute('ROLLBACK');
      rethrow;
    }
  }

  List<Reel> pendingUploads() => [
        for (final r in db.select('SELECT data FROM reels WHERE pending_upload = 1'))
          Reel.fromJson(jsonDecode(r['data'] as String) as Map<String, dynamic>)
      ];

  int get reelCount => db.select('SELECT count(*) AS n FROM reels').first['n'] as int;

  /// Sign-out / account deletion: nothing of this person stays on the phone.
  void clearAll() {
    for (final t in ['reels', 'reels_fts', 'jobs', 'chats']) {
      db.execute('DELETE FROM $t');
    }
  }

  void deleteReel(String shortcode) {
    db.execute('DELETE FROM reels WHERE shortcode = ?', [shortcode]);
    db.execute('DELETE FROM reels_fts WHERE shortcode = ?', [shortcode]);
    db.execute('DELETE FROM chats WHERE shortcode = ?', [shortcode]);
  }

  List<Reel> recent({int limit = 50, int offset = 0}) => [
        for (final r in db.select('SELECT data FROM reels ORDER BY saved_at DESC LIMIT ? OFFSET ?', [limit, offset]))
          Reel.fromJson(jsonDecode(r['data'] as String) as Map<String, dynamic>)
      ];

  /// Best matches first; any word may match (same behaviour as the bot's /ask).
  List<Reel> search(String query, {int limit = 3}) {
    final terms = RegExp(r'\w+', unicode: true)
        .allMatches(query.toLowerCase())
        .map((m) => m.group(0)!)
        .where((t) => t.length > 1 && !_stopwords.contains(t))
        .take(12)
        .toList();
    if (terms.isEmpty) return [];
    final match = terms.map((t) => '"$t"').join(' OR ');
    final rows = db.select(
      'SELECT r.data FROM reels_fts f JOIN reels r USING (shortcode) '
      'WHERE reels_fts MATCH ? ORDER BY bm25(reels_fts), r.saved_at DESC LIMIT ?',
      [match, limit],
    );
    return [for (final r in rows) Reel.fromJson(jsonDecode(r['data'] as String) as Map<String, dynamic>)];
  }

  static const _stopwords = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'can', 'do', 'does', 'for', 'from', 'how',
    'in', 'is', 'it', 'me', 'my', 'of', 'on', 'or', 'so', 'that', 'the', 'this', 'to', 'was', 'what',
    'when', 'where', 'which', 'who', 'why', 'will', 'with', 'you', 'your', 'about', 'any', 'all',
  };

  // ---------------------------------------------------------------- jobs

  /// Queue a reel. Returns the existing job if this reel is already queued or running.
  Job enqueue({required String shortcode, String? url, String? filePath}) {
    final existing = db.select(
      "SELECT * FROM jobs WHERE shortcode = ? AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
      [shortcode],
    );
    if (existing.isNotEmpty) return _job(existing.first);
    db.execute(
      'INSERT INTO jobs (shortcode, url, file_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
      [shortcode, url, filePath, _now(), _now()],
    );
    return getJob(db.lastInsertRowId)!;
  }

  Job? getJob(int id) {
    final rows = db.select('SELECT * FROM jobs WHERE id = ?', [id]);
    return rows.isEmpty ? null : _job(rows.first);
  }

  /// Oldest queued job, marked running. Jobs left "running" by a killed app are
  /// put back first (only one app process ever runs the queue).
  Job? claimNext() {
    final rows = db.select("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1");
    if (rows.isEmpty) return null;
    final id = rows.first['id'] as int;
    db.execute(
      "UPDATE jobs SET status = 'running', attempts = attempts + 1, step = '', updated_at = ? WHERE id = ?",
      [_now(), id],
    );
    return getJob(id);
  }

  /// Called once at startup: anything "running" was interrupted when the app was killed.
  int requeueInterrupted() {
    db.execute("UPDATE jobs SET status = 'queued', step = '', updated_at = ? WHERE status = 'running'", [_now()]);
    return db.updatedRows;
  }

  void setStep(int id, String step) =>
      db.execute('UPDATE jobs SET step = ?, updated_at = ? WHERE id = ?', [step, _now(), id]);

  void finishJob(int id) =>
      db.execute("UPDATE jobs SET status = 'done', step = '', error = NULL, updated_at = ? WHERE id = ?", [_now(), id]);

  void failJob(int id, String error) => db.execute(
        "UPDATE jobs SET status = 'failed', step = '', error = ?, updated_at = ? WHERE id = ?",
        [error.length > 500 ? error.substring(0, 500) : error, _now(), id],
      );

  /// Retry a failed job, optionally with a video file the user picked.
  void retryJob(int id, {String? filePath}) => db.execute(
        "UPDATE jobs SET status = 'queued', error = NULL, step = '', attempts = 0, server_job_id = NULL, "
        'file_path = COALESCE(?, file_path), updated_at = ? WHERE id = ?',
        [filePath, _now(), id],
      );

  /// Jobs to show in the UI: everything not done, newest first.
  List<Job> activeJobs() => [
        for (final r in db.select("SELECT * FROM jobs WHERE status <> 'done' ORDER BY id DESC LIMIT 50")) _job(r)
      ];

  void dismissJob(int id) => db.execute('DELETE FROM jobs WHERE id = ?', [id]);

  void setServerJob(int id, int? serverJobId) =>
      db.execute('UPDATE jobs SET server_job_id = ?, updated_at = ? WHERE id = ?', [serverJobId, _now(), id]);

  Job _job(Row r) => Job(
        id: r['id'] as int,
        shortcode: r['shortcode'] as String,
        url: r['url'] as String?,
        filePath: r['file_path'] as String?,
        status: JobStatus.values.byName(r['status'] as String),
        step: r['step'] as String,
        error: r['error'] as String?,
        attempts: r['attempts'] as int,
        serverJobId: r['server_job_id'] as int?,
      );

  // ---------------------------------------------------------------- chat

  List<Map<String, String>> chatHistory(String shortcode) {
    final rows = db.select('SELECT history FROM chats WHERE shortcode = ?', [shortcode]);
    if (rows.isEmpty) return [];
    return [
      for (final m in jsonDecode(rows.first['history'] as String) as List)
        {'role': m['role'] as String, 'content': m['content'] as String}
    ];
  }

  void saveChatHistory(String shortcode, List<Map<String, String>> history) => db.execute(
        'INSERT INTO chats (shortcode, history) VALUES (?, ?) '
        'ON CONFLICT (shortcode) DO UPDATE SET history = excluded.history',
        [shortcode, jsonEncode(history)],
      );

  void dispose() => db.close();
}
