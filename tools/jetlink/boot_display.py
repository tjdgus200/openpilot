"""CPU-rendered USB diagnosis, independent of comma packets, model and Xorg."""
from io import BytesIO
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'openpilot/selfdrive/carrot/cluster'))
from boot_status import collect
from display_owner import hud_requested, panel_owner


def lines(status):
  units = status.get('units', {})
  failed = [name for name, value in units.items() if value.get('ActiveState') == 'failed']
  stage = status.get('stage', {})
  storage = status.get('storage', {}).get('state', 'waiting')
  update = status.get('update', {})
  error = bool(failed or stage.get('error') or storage == 'base-recovery' or update.get('state') == 'failed')
  title = ('부팅 상태를 확인해 주세요', 'BOOT NEEDS ATTENTION') if error else ('젯슨 연결 준비 중', 'JETSON STARTING / WAITING')
  rows = [
    ('시스템 보호', 'SYSTEM STORAGE', storage),
    ('부팅 단계', 'BOOT STAGE', stage.get('stage', 'starting')),
    ('무선 네트워크', 'WI-FI', str(status.get('wifi', 'waiting')) +
     (' / ' + status['wifi_reason'] if status.get('wifi_reason') else '')),
    ('접속 주소', 'IP ADDRESS', ' / '.join(status.get('addresses', [])) or 'Waiting for network'),
    ('원격 접속', 'SSH', units.get('ssh', {}).get('ActiveState', 'waiting')),
    ('콤마 USB 서비스', 'COMMA USB SERVICE', units.get('carrot-jetlink', {}).get('ActiveState', 'waiting')),
  ]
  detail = ', '.join(failed[:3]) or stage.get('error', '')
  states = {'checking': '릴리스 확인 대기', 'receiving': 'USB 수신 중', 'received': '수신 완료 · 설치 대기',
            'waiting_usb': 'USB 재연결 대기', 'downloading': '다운로드 중', 'waiting_internet': '인터넷 대기',
            'verifying': '검증 / 설치 중', 'ready': '실행 준비 완료', 'failed': '업데이트 실패'}
  state = update.get('state')
  summary = states.get(state, '콤마 업데이트 요청 대기')
  if update.get('stale'):
    summary += ' (상태 갱신 중단)'
  fraction = update.get('fraction')
  if state in ('receiving', 'downloading') and type(fraction) in (float, int) and math.isfinite(fraction) and 0 <= fraction <= 1:
    summary += f' {fraction * 100:.1f}%'
  data = status.get('data', {})
  space = (f"{data['free_bytes'] / 1e9:.2f} / {data['total_bytes'] / 1e9:.2f} GB free"
           if 'free_bytes' in data and 'total_bytes' in data else '용량 확인 대기')
  code = update.get('error_code')
  reasons = {'insufficient_space': 'DATA 여유 공간 부족', 'permission_denied': '업데이트 파일 접근 권한 오류',
             'package_rejected': '패키지 수락 검사 실패', 'activation_failed': '설치 / 실행 검사 실패',
             'usb_disconnected': 'USB 연결 중단 · 다시 연결해 주세요'}
  if code in reasons:
    detail = reasons[code]
    if code == 'insufficient_space' and 'required_bytes' in update:
      detail += f" · 필요 {update['required_bytes'] / 1e9:.2f} GB"
  elif not detail:
    detail = {'checking': '콤마가 설치할 릴리스를 확인하고 있습니다',
              'received': '파일 수신 완료 · 검증과 실행 검사 후 계기판을 시작합니다',
              'verifying': '검증과 실행 검사 완료 전까지 모델과 계기판은 대기합니다',
              'waiting_internet': '다운로드에 필요한 인터넷 연결을 기다립니다',
              'ready': '업데이트 확인 완료 · 계기판 시작을 기다립니다'}.get(state, '')
  phase = {'begin': '전송 시작 전', 'transfer': '파일 전송', 'verify': '파일 검증'}.get(update.get('phase'), '--')
  rows += [('업데이트', 'UPDATE', summary), ('DATA 여유 / 전체', 'STORAGE', space),
           ('업데이트 검사 단계', 'UPDATE PHASE', phase)]
  return title, rows, detail, error


def render(status, root=ROOT, size=(1920, 462)):
  from PIL import Image, ImageDraw, ImageFont
  image = Image.new('RGB', size, '#101821')
  draw = ImageDraw.Draw(image)
  font_path = root / 'openpilot/selfdrive/assets/fonts/KaiGenGothicKR-Bold.ttf'
  font = lambda n: ImageFont.truetype(str(font_path), n)
  title, rows, detail, error = lines(status)
  accent = '#ff8876' if error else '#65d7bb'
  draw.rectangle((0, 0, 12, size[1]), fill=accent)
  draw.text((38, 20), title[0], font=font(36), fill='white')
  draw.text((40, 65), title[1], font=font(19), fill=accent)
  health = [f"BOOT +{status.get('uptime', 0)}s"]
  for name, unit in (('power_w', 'W'), ('temp_c', 'C')):
    value = status.get(name)
    health.append(f'{value:.1f} {unit}' if type(value) in (int, float) and math.isfinite(value) else f'-- {unit}')
  draw.text((size[0] - 40, 34), '  |  '.join(health), anchor='ra', font=font(22), fill=accent)
  for index, (ko, en, value) in enumerate(rows):
    x, y = 40 + (index % 3) * 625, 100 + (index // 3) * 88
    draw.text((x, y), ko, font=font(23), fill='#dce7f1')
    draw.text((x, y+29), en, font=font(13), fill='#8198ae')
    value_font = font(20)
    value = str(value)
    while value and draw.textlength(value, font=value_font) > 585:
      value = value[:-4] + '...' if len(value) > 4 else ''
    draw.text((x, y+48), value, font=value_font, fill=accent)
  footer = detail or '콤마 연결과 계기판 화면 준비를 기다립니다'
  draw.text((40, 373), footer[:105], font=font(22), fill=accent)
  draw.text((40, 410), 'Waiting for comma / dashboard. This is a status screen, not driving guidance.',
            font=font(17), fill='#97a9bc')
  return image


def panel_frame(frame):
  from PIL import Image
  # Match cluster_renderer portrait_upload's render/readback transform.
  return frame.transpose(Image.Transpose.ROTATE_270)


def main():
  import cluster_usb_display as usb
  import openpilot.common.usbgpu_bus_lock as bus
  # RAM path exists even if the storage setup failed before mounting /tmp.
  bus.USBGPU_BUS_LOCK_PATH = '/run/carrot-jetlink-display/usb.lock'
  usb._set_cluster_hud_connected = lambda value: None
  usb._usbgpu_reset_protected = lambda: True  # Diagnostics never reset the shared USB path.
  class Panel(usb.TuringUsbDisplay):
    def close(self):
      # Preserve the last diagnostic image until the HUD sends its first frame.
      self._disconnected = True
      super().close()
  while True:
    with panel_owner() as acquired:
      if acquired and usb.find_supported_usb_product(0x0092) is not None:
        panel = Panel(brightness=65, display_fps=1, expected_product_id=0x0092)
        try:
          panel.open()
          while not hud_requested():
            frame = render(collect(), size=(panel.landscape_width, panel.landscape_height))
            stream = BytesIO()
            panel_frame(frame).save(stream, format='JPEG', quality=82)
            panel.send_jpeg(stream.getvalue())
            time.sleep(1)
        except Exception as error:
          print('Boot display retry:', type(error).__name__, flush=True)
        finally:
          panel.close()
    time.sleep(1)


if __name__ == '__main__':
  main()
