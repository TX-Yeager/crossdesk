"""Safe, stable session reasons shared by the router, UI and peer protocol."""

STOP_REASONS = {
    'manual': '手动暂停',
    'f12': '按下 F12 暂停',
    'peer_pause': '对方暂停互控',
    'disconnect': '连接断开，请重新连接后开始',
    'start_timeout': '对方未及时确认开始互控',
    'pause_timeout': '本机已暂停，对方未及时确认',
    'permission_required': '系统输入权限未授权',
    'capture_failed': '系统输入捕获不可用',
    'position_failed': '系统光标定位失败',
    'input_restore_failed': '本机光标或应用焦点恢复失败',
    'input_queue_full': '输入队列已满',
    'routing_error': '输入处理异常',
    'layout_changed': '显示器布局已变化，请确认后重新开始',
    'app_exit': 'CrossDesk 已退出',
}


def stop_reason(code, source='local'):
    code = code if isinstance(code, str) and code in STOP_REASONS else 'peer_pause' if source == 'peer' else 'routing_error'
    return {'code': code, 'source': source,
            'message': ('对方：' if source == 'peer' else '本机：') + STOP_REASONS[code]}
