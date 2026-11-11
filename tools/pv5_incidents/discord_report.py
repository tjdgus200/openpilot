"""PC-only Discord delivery, with a durable receipt and no ambiguous blind resend."""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

from openpilot.system.pv5_incidents.core import CONDITIONS, encode


class Discord:
  def __init__(self, config):
    self.channel = config['discord_channel']
    self.guild = config['discord_guild']
    bridge = Path(config['discord_bridge'])
    authority = json.loads((bridge / '.data/authorization.json').read_text(encoding='utf-8'))
    if authority['guildId'] != self.guild or config['discord_user'] not in authority['userIds']:
      raise ValueError('destination_not_authorized')
    entries = (bridge / '.env').read_text(encoding='utf-8-sig').splitlines()
    tokens = [line.partition('=')[2].strip().strip('\"\'') for line in entries if line.startswith('DISCORD_BOT_TOKEN=')]
    if len(tokens) != 1 or not tokens[0]:
      raise ValueError('bot_configuration_missing')
    self.token = tokens[0]
    self.receipt = Path(config['discord_receipt'])
    self.bot = None

  def request(self, route, body=None):
    data = None if body is None else encode(body)
    request = Request('https://discord.com/api/v10' + route, data=data, headers={
      'Authorization': 'Bot ' + self.token, 'Content-Type': 'application/json', 'User-Agent': 'PV5-Incident-Report/1'})
    with urlopen(request, timeout=5.) as response:
      raw = response.read(262145)
    if len(raw) > 262144:
      raise ValueError('response_too_large')
    return json.loads(raw)

  def save(self, receipt):
    self.receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = self.receipt.with_suffix('.tmp')
    temporary.write_bytes(encode(receipt))
    temporary.replace(self.receipt)

  def send(self, content, digests, purpose):
    # The receiver receipt notifier and the Codex heartbeat are separate PC
    # processes. OS locks release on crashes and preserve uncertain POST state.
    self.receipt.parent.mkdir(parents=True, exist_ok=True)
    with self.receipt.with_suffix('.lock').open('a+b') as lock:
      lock.seek(0, 2)
      if lock.tell() == 0:
        lock.write(b'0'); lock.flush()
      lock.seek(0)
      if os.name == 'nt':
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
      else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      try:
        return self.send_locked(content, digests, purpose)
      finally:
        lock.seek(0)
        if os.name == 'nt':
          msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
          fcntl.flock(lock, fcntl.LOCK_UN)

  def send_locked(self, content, digests, purpose):
    if not 1 <= len(content) <= 1900 or len(digests) > 8 or purpose not in ('received', 'analysis'):
      raise ValueError('invalid_report')
    fingerprint = hashlib.sha256(encode([purpose, digests, content])).hexdigest()
    pending = None
    if self.receipt.exists():
      if self.receipt.stat().st_size > 16384:
        raise ValueError('invalid_receipt')
      pending = json.loads(self.receipt.read_bytes())
      if pending.get('verified') and pending['fingerprint'] == fingerprint:
        return pending
      if pending.get('verified'):
        pending = None
      elif pending.get('fingerprint') != fingerprint:
        # Finish the earlier uncertain transmission first, never overwrite it.
        return self.reconcile(pending)
    if pending is None:
      if self.request('/channels/' + self.channel).get('guild_id') != self.guild:
        raise ValueError('channel_mismatch')
      pending = {'fingerprint': fingerprint, 'nonce': fingerprint[:24], 'content': content,
                 'digests': digests, 'purpose': purpose, 'created': int(time.time()), 'verified': False}
      self.save(pending)  # A POST timeout must still leave a durable reconciliation task.
      result = self.request('/channels/' + self.channel + '/messages', {
        'content': content, 'allowed_mentions': {'parse': []}, 'enforce_nonce': True, 'nonce': pending['nonce']})
      pending['message_id'] = result['id']; self.save(pending)
    return self.reconcile(pending)

  def reconcile(self, pending):
    if 'message_id' not in pending:
      if self.bot is None:
        self.bot = self.request('/users/@me')['id']
      messages = self.request('/channels/' + self.channel + '/messages?limit=50')
      found = [m for m in messages if m.get('author', {}).get('id') == self.bot
               and m.get('content') == pending['content']]
      if not found:
        raise ValueError('delivery_uncertain_reconcile_before_resend')
      pending['message_id'] = found[0]['id']; self.save(pending)
    message = self.request('/channels/' + self.channel + '/messages/' + pending['message_id'])
    if message['channel_id'] != self.channel or message['content'] != pending['content'] or message.get('attachments'):
      raise ValueError('receipt_mismatch')
    pending['verified'] = True; self.save(pending)
    return pending


def notify_once(inbox, discord):
  rows = inbox.notifications()
  if not rows:
    return False
  lines = []
  for row in rows:
    fact = row['fact']
    condition = '상황성 경고·결함 여부 미확정' if fact['code'] in CONDITIONS else '원인 분석 대기'
    lines.append(f"- {fact['process']} / {fact['code']} · {row['total']}회 · {condition}")
  content = '**PV5 오류 제보 수신**\n' + '\n'.join(lines) + '\nPC 자동 분석에서 근거와 해결안을 이어서 보고합니다. 자동 코드 수정·배포는 하지 않습니다.'
  receipt = discord.send(content, [row['digest'] for row in rows], 'received')
  if receipt['purpose'] == 'received' and receipt['verified']:
    inbox.mark_notified(receipt['digests'])
  return True
