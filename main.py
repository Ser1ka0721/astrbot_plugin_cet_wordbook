import asyncio
import datetime
import hashlib
import json
import os
import random
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

DATA = '/AstrBot/data/plugin_data/cet_wordbook'
CFG_FILE = '/AstrBot/data/config/astrbot_plugin_cet_wordbook_config.json'
BOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'books')
STATE_FILE = os.path.join(DATA, 'state.json')
WRONG_FILE = os.path.join(DATA, 'wrongbook.json')
BOOKS = {'四级': 'cet4.json', '六级': 'cet6.json'}

DEFAULTS = {
    'wordbook': '四级',
    'words_per_day': 20,
    'send_mode': '每小时',
    'interval_value': 1,
    'rest_start': 8,
    'rest_end': 22,
    'quizzes_per_day': 3,
    'quiz_batch': 1,
    'wrongbook_days': 7,
    'quiz_source': '整本词书',
    'quiz_time': 0,
    'target_session': '',
}


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def phone(w):
    p = (w.get('usphone') or w.get('ukphone') or '').strip()
    return ('/' + p.replace("'", 'ˈ') + '/') if p else ''


def meaning(w):
    s = '；'.join(x.strip() for x in (w.get('trans') or []) if x)
    return re.sub(r'([，；、。：])\s+', r'\1', s)


def today_str():
    return datetime.datetime.now().strftime('%Y-%m-%d')


@register("astrbot_plugin_cet_wordbook", "local", "四六级词书", "1.0.0")
class CetWordbook(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        os.makedirs(DATA, exist_ok=True)
        self._books = {}
        self._task = None

    async def initialize(self):
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        if self._task:
            self._task.cancel()

    def cfg(self):
        d = dict(DEFAULTS)
        src = dict(self.config or {})
        src.update(load_json(CFG_FILE, {}) or {})
        for k, v in src.items():
            if k not in DEFAULTS:
                continue
            if v is None or v == '':
                continue
            d[k] = v
        for k in ('words_per_day', 'interval_value', 'rest_start', 'rest_end',
                  'quizzes_per_day', 'quiz_batch', 'wrongbook_days', 'quiz_time'):
            try:
                d[k] = int(d[k])
            except Exception:
                d[k] = DEFAULTS[k]
        if d['wordbook'] not in BOOKS:
            d['wordbook'] = '四级'
        if d.get('quiz_source') not in ('今日单词', '整本词书'):
            d['quiz_source'] = '整本词书'
        return d

    def save_cfg(self, patch):
        cur = load_json(CFG_FILE, {}) or {}
        cur.update(patch)
        save_json(CFG_FILE, cur)

    def book(self, name):
        name = name if name in BOOKS else '四级'
        if name not in self._books:
            self._books[name] = load_json(os.path.join(BOOK_DIR, BOOKS[name]), [])
        return self._books[name]

    def window(self, cfg):
        start = cfg['rest_start'] % 24
        end = cfg['rest_end'] % 24
        total = ((end - start) % 24) * 60
        if total <= 0:
            total = 1440
        return start * 60, total

    def step(self, cfg):
        mode = cfg['send_mode']
        v = max(1, cfg['interval_value'])
        if mode == '每N小时':
            return 60 * v
        if mode == '每N分钟':
            return v
        return 60

    def build(self, cfg):
        start, total = self.window(cfg)
        step = max(1, self.step(cfg))
        slots = max(1, total // step)
        words = max(1, cfg['words_per_day'])
        base, extra = divmod(words, slots)
        word_slots = []
        for i in range(slots):
            n = base + (1 if i < extra else 0)
            if n <= 0:
                continue
            word_slots.append({'t': (start + i * step) % 1440, 'n': n, 'done': False})

        quiz_slots = []
        q = max(0, cfg['quizzes_per_day'])
        batch = max(1, cfg['quiz_batch'])
        fixed = int(cfg.get('quiz_time') or 0)
        if q and fixed:
            minute = (fixed % 24) * 60
            inside = ((minute - start) % 1440) < total
            if inside:
                quiz_slots.append({'t': minute, 'n': min(10, q), 'done': False})
        elif q and batch:
            count = (q + batch - 1) // batch
            qstep = max(1, total // max(1, count))
            for i in range(count):
                n = min(batch, q - i * batch)
                if n <= 0:
                    continue
                quiz_slots.append({'t': (start + i * qstep) % 1440, 'n': n, 'done': False})
        return word_slots, quiz_slots

    def sig(self, cfg):
        keys = ['wordbook', 'words_per_day', 'send_mode', 'interval_value',
                'rest_start', 'rest_end', 'quizzes_per_day', 'quiz_batch',
                'quiz_source', 'quiz_time']
        raw = json.dumps({k: cfg[k] for k in keys}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def in_window(self, cfg, minute):
        start, total = self.window(cfg)
        return ((minute - start) % 1440) < total

    # ---------- 调度 ----------

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception:
                pass
            await asyncio.sleep(20)

    async def _tick(self):
        now = datetime.datetime.now()
        cfg = self.cfg()
        state = load_json(STATE_FILE, {}) or {}
        sig = self.sig(cfg)
        cur = now.hour * 60 + now.minute
        if state.get('date') != today_str() or state.get('sig') != sig:
            ws, qs = self.build(cfg)
            for s in ws:
                s['done'] = s['t'] < cur
            for s in qs:
                s['done'] = s['t'] < cur
            state['date'] = today_str()
            state['sig'] = sig
            state['word_slots'] = ws
            state['quiz_slots'] = qs
            state.setdefault('queue', [])
            state.setdefault('pending', [])
            if state.get('date') != today_str():
                state['sent_words'] = []
            else:
                state.setdefault('sent_words', [])
            state.setdefault('last_digest', today_str())
            save_json(STATE_FILE, state)

        if not self.in_window(cfg, cur):
            return
        sess = cfg.get('target_session') or state.get('session') or ''
        if not sess:
            return

        changed = False
        for slot in state.get('word_slots', []):
            if slot.get('done'):
                continue
            if slot['t'] <= cur:
                slot['done'] = True
                changed = True
                if cur - slot['t'] <= 180:
                    words = self.next_words(cfg, state, slot['n'])
                    text = self.render_words(cfg, words)
                    if text:
                        await self.send(sess, text)
                        state.setdefault('sent_words', [])
                        state['sent_words'].extend(w.get('name', '') for w in words)
        for slot in state.get('quiz_slots', []):
            if slot.get('done'):
                continue
            if slot['t'] <= cur:
                slot['done'] = True
                changed = True
                if cur - slot['t'] <= 180:
                    qs = self.make_quiz(cfg, state, slot['n'])
                    if qs:
                        await self.send(sess, self.render_quiz(qs))
        if changed:
            save_json(STATE_FILE, state)

        if self.digest_due(cfg, state):
            text = self.render_wrongbook()
            if text:
                await self.send(sess, text)
            state['last_digest'] = today_str()
            save_json(STATE_FILE, state)

    async def send(self, session, text):
        await self.context.send_message(session, MessageChain([Plain(text)]))

    # ---------- 内容 ----------

    def next_words(self, cfg, state, n):
        book = self.book(cfg['wordbook'])
        if not book:
            return []
        queue = state.get('queue') or []
        out = []
        for _ in range(n):
            if not queue:
                queue = list(range(len(book)))
                random.shuffle(queue)
            i = queue.pop()
            if 0 <= i < len(book):
                out.append(book[i])
        state['queue'] = queue
        return out

    def render_words(self, cfg, words):
        if not words:
            return ''
        lines = ['📖 %s词汇' % cfg['wordbook'], '']
        for w in words:
            line = w.get('name', '')
            p = phone(w)
            m = meaning(w)
            if p:
                line += ' ' + p
            if m:
                line += '\n' + m
            lines.append(line)
            lines.append('')
        return '\n'.join(lines).strip()

    def make_quiz(self, cfg, state, n):
        book = self.book(cfg['wordbook'])
        if not book:
            return []
        pending = [p for p in state.get('pending', []) if time.time() - p.get('t', 0) < 10800]
        state['pending'] = pending
        pool = book
        if cfg.get('quiz_source') == '今日单词':
            names = set(state.get('sent_words') or [])
            today_words = [w for w in book if w.get('name') in names]
            if today_words:
                pool = today_words
        picks = random.sample(pool, k=min(max(1, n), len(pool)))
        while len(picks) < max(1, n):
            picks.append(random.choice(pool))
        qs = []
        for w in picks:
            correct = meaning(w)
            if not correct:
                continue
            opts = [correct]
            guard = 0
            while len(opts) < 4 and guard < 60:
                guard += 1
                d = random.choice(book)
                m = meaning(d)
                if m and m not in opts:
                    opts.append(m)
            random.shuffle(opts)
            letter = 'ABCD'[opts.index(correct)]
            item = {'w': w.get('name', ''), 'm': correct, 'opts': opts,
                    'a': letter, 't': time.time(), 'book': cfg['wordbook']}
            qs.append(item)
            state.setdefault('pending', []).append(item)
        return qs

    def render_quiz(self, qs):
        lines = ['📝 小测']
        for i, q in enumerate(qs, 1):
            lines.append('')
            lines.append('%d. %s 的中文意思是？' % (i, q['w']))
            for j, opt in enumerate(q['opts']):
                lines.append('   %s. %s' % ('ABCD'[j], opt))
        lines.append('')
        lines.append('回复答案，例如 1A 2B')
        return '\n'.join(lines)

    def digest_due(self, cfg, state):
        days = cfg['wrongbook_days']
        if days <= 0:
            return False
        wrong = load_json(WRONG_FILE, {}) or {}
        if not wrong.get('items'):
            return False
        last = state.get('last_digest') or today_str()
        try:
            d0 = datetime.date.fromisoformat(last)
        except Exception:
            return False
        return (datetime.date.today() - d0).days >= days

    def render_wrongbook(self):
        wrong = (load_json(WRONG_FILE, {}) or {}).get('items', {})
        if not wrong:
            return '📕 错题本还是空的'
        items = sorted(wrong.items(), key=lambda kv: kv[1].get('last', ''), reverse=True)
        lines = ['📕 错题本（共 %d 词）' % len(items)]
        for name, info in items[:30]:
            lines.append('')
            lines.append('%s  %s' % (name, info.get('m', '')))
        if len(items) > 30:
            lines.append('')
            lines.append('……还有 %d 个' % (len(items) - 30))
        return '\n'.join(lines)

    # ---------- 答题与错题 ----------

    def add_wrong(self, name, m, book):
        wrong = load_json(WRONG_FILE, {}) or {}
        items = wrong.setdefault('items', {})
        info = items.get(name) or {'m': m, 'book': book, 'count': 0}
        info['m'] = m or info.get('m', '')
        info['book'] = book or info.get('book', '')
        info['count'] = int(info.get('count', 0)) + 1
        info['last'] = time.strftime('%Y-%m-%d %H:%M')
        items[name] = info
        save_json(WRONG_FILE, wrong)

    def requeue(self, book_name, name):
        book = self.book(book_name)
        for i, w in enumerate(book):
            if w.get('name') == name:
                state = load_json(STATE_FILE, {}) or {}
                q = state.get('queue') or []
                q.append(i)
                random.shuffle(q)
                state['queue'] = q
                save_json(STATE_FILE, state)
                return

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        state = load_json(STATE_FILE, {}) or {}
        state['session'] = event.unified_msg_origin
        save_json(STATE_FILE, state)
        text = (event.message_str or '').strip()
        if not text or text.startswith('/'):
            return
        pending = state.get('pending') or []
        if not pending:
            return
        letters = re.findall(r'[A-Da-d]', text)
        if not letters:
            return
        results = []
        remaining = []
        for i, q in enumerate(pending):
            if i >= len(letters):
                remaining.append(q)
                continue
            got = letters[i].upper()
            if got == q['a']:
                results.append('✅ %s 对了' % q['w'])
            else:
                results.append('❌ %s 错了，正确答案是 %s：%s' % (q['w'], q['a'], q['m']))
                self.add_wrong(q['w'], q['m'], q.get('book') or self.cfg()['wordbook'])
                self.requeue(q.get('book') or self.cfg()['wordbook'], q['w'])
        state['pending'] = remaining
        save_json(STATE_FILE, state)
        if results:
            yield event.plain_result('📝 答题结果\n\n' + '\n'.join(results))

    # ---------- 指令 ----------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('词书')
    async def cmd_book(self, event: AstrMessageEvent):
        arg = (event.message_str or '').partition(' ')[2].strip()
        if arg in ('四', '四级', '4', 'cet4'):
            self.save_cfg({'wordbook': '四级'})
            yield event.plain_result('已切换到四级词书')
        elif arg in ('六', '六级', '6', 'cet6'):
            self.save_cfg({'wordbook': '六级'})
            yield event.plain_result('已切换到六级词书')
        else:
            yield event.plain_result('当前词书：%s\n用法：/词书 四 或 /词书 六' % self.cfg()['wordbook'])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('错题本')
    async def cmd_wrong(self, event: AstrMessageEvent):
        yield event.plain_result(self.render_wrongbook())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('小测')
    async def cmd_quiz(self, event: AstrMessageEvent):
        cfg = self.cfg()
        state = load_json(STATE_FILE, {}) or {}
        n = cfg['quiz_batch']
        arg = (event.message_str or '').partition(' ')[2].strip()
        if arg.isdigit():
            n = max(1, min(5, int(arg)))
        qs = self.make_quiz(cfg, state, n)
        state['session'] = event.unified_msg_origin
        save_json(STATE_FILE, state)
        yield event.plain_result(self.render_quiz(qs))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('单词')
    async def cmd_word(self, event: AstrMessageEvent):
        cfg = self.cfg()
        state = load_json(STATE_FILE, {}) or {}
        arg = (event.message_str or '').partition(' ')[2].strip()
        n = max(1, min(10, int(arg))) if arg.isdigit() else 1
        words = self.next_words(cfg, state, n)
        state['session'] = event.unified_msg_origin
        state.setdefault('sent_words', [])
        state['sent_words'].extend(w.get('name', '') for w in words)
        save_json(STATE_FILE, state)
        yield event.plain_result(self.render_words(cfg, words))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('词书状态')
    async def cmd_status(self, event: AstrMessageEvent):
        cfg = self.cfg()
        state = load_json(STATE_FILE, {}) or {}
        start, total = self.window(cfg)
        yield event.plain_result(
            '📚 四六级词书\n'
            '词书：%s\n每天：%d 词\n间隔：%s（%d 分钟）\n活跃：%02d:00 - %02d:00\n小测：每天 %d 题\n错题本推送：每 %d 天\n今日已发：%d / %d' % (
                cfg['wordbook'], cfg['words_per_day'], cfg['send_mode'], self.step(cfg),
                cfg['rest_start'], cfg['rest_end'], cfg['quizzes_per_day'],
                cfg['quiz_source'],
                ('，固定在 %d 点' % cfg['quiz_time']) if cfg.get('quiz_time') else '，均匀分布',
                cfg['wrongbook_days'],
                sum(s.get('n', 0) for s in state.get('word_slots', []) if s.get('done')),
                cfg['words_per_day']))
