"""Conservative destination diagnostics: preview by default, no sends or joins."""
from . import db, telegram_ops


def permanent_failure(error: str) -> bool:
    # Allowlist only: timeouts, FloodWait, network and unknown errors defer.
    text = str(error).lower()
    return any(code in text for code in (
        'chatwriteforbidden', 'chat_write_forbidden', 'chatadminrequired',
        'chat_admin_required', 'channelprivate', 'channel_private',
        'usernameinvalid', 'username_invalid', 'usernamenotoccupied',
        'username_not_occupied', 'userbannedinchannel', 'user_banned_in_channel',
        'you can\'t write in this chat',
    ))


async def scan(*, resolve=False, apply=False) -> list[dict]:
    rows = []
    for channel in db.list_channels():
        if channel['platform'] != 'telegram':
            continue
        result = {'id': channel['id'], 'status': channel['status'], 'action': 'unchanged'}
        identifier = str(channel['identifier']).strip()
        reason = ''
        if not identifier:
            reason = 'empty destination identifier'
        elif resolve:
            try:
                lookup = await telegram_ops.inspect_channel(identifier)
            except Exception as exc:
                lookup = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}
            if lookup.get('ok') and lookup.get('can_post') is False:
                reason = 'Telegram confirms no posting permission'
            elif not lookup.get('ok'):
                if permanent_failure(lookup.get('error', '')):
                    reason = lookup['error']
                else:
                    result['action'] = 'deferred'
                    result['reason'] = lookup.get('error', 'lookup unavailable')
        if reason:
            result.update(action='would_pause', reason=reason)
            if apply and channel['status'] == 'active':
                db.update_channel_details(channel['id'], status='paused')
                db.record_audit(actor='doctor', role='system', action='channel_fix',
                                target=str(channel['id']), detail=reason)
                result['action'] = 'paused'
        rows.append(result)
    return rows
