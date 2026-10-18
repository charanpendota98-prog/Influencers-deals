import asyncio
import json

import pytest

from influencer_hub import category_preferences as prefs
from influencer_hub import config, db, pipeline

PHONE = 'Samsung smartphone ₹9999 https://www.amazon.in/dp/B012345678?tag=source-21'
SHIRT = 'Cotton shirt ₹499 https://www.amazon.in/dp/B012345679?tag=source-21'


def profile(spec='electronics', **rules):
    return {'categories': spec, 'category_preferences': json.dumps({'mode': 'strict', **rules})}


@pytest.mark.parametrize('text,spec,passes', [
    ('Smartphone 5G', 'electronics', True), ('Cotton shirt', 'electronics', False),
    ('Mystery ZXQ1999', 'electronics', False), ('Use coupon code SAVE', 'electronics', False),
    ('Shoes', 'footwear', True), ('Gaming mouse', 'electronics', True),
    ('Gaming mouse', 'gaming', True), ('Air fryer', 'home', True),
    ('Samsung refrigerator', 'appliances', True), ('Samsung', 'electronics', False),
    ('Shirt with shoes', 'footwear', False), ('Phone and cotton shirt', 'electronics', False),
    ('Phone and cotton shirt', 'electronics,clothing', True),
    ('Paperback book', 'books', True), ('Pampers diapers', 'toys', True),
    ('Use recharge coupon code SAVE', 'recharge_freebies', True),
])
def test_selected_products_only(text, spec, passes):
    assert prefs.evaluate(text, profile(spec))['passes'] is passes


def test_exclusions_keywords_and_mixed_mode():
    assert prefs.evaluate('Phone and shirt', profile(mixed='allow'))['passes']
    assert not prefs.evaluate('Phone and shirt', profile(mixed='allow', excluded_categories=['clothing']))['passes']
    assert not prefs.evaluate('Refurbished phone', profile(blocked_keywords=['refurbished']))['passes']
    assert prefs.evaluate('Smartphone', profile(required_keywords=['phone', 'smartphone']))['passes']
    assert not prefs.evaluate('Headphones', profile(required_keywords=['phone']))['passes']
    assert not prefs.evaluate('Earbuds', profile(required_keywords=['smartphone']))['passes']
    assert not prefs.evaluate('Phone https://example.com/refurbished', profile(required_keywords=['refurbished']))['passes']
    assert prefs.evaluate('Phone\nFollow refurbished electronics channel', profile(blocked_keywords=['refurbished']))['passes']


def test_channel_can_only_narrow_strict_creator():
    assert not prefs.evaluate(SHIRT, profile(), {'categories': 'all'})['passes']
    assert not prefs.evaluate(PHONE, profile(), {'categories': 'clothing'})['passes']
    assert prefs.evaluate(PHONE, profile(), {'categories': 'all'})['passes']
    assert prefs.evaluate(PHONE, profile(), {'categories': ''})['passes']
    assert not prefs.evaluate(PHONE, profile(), {'categories': 'electrnics'})['passes']


def test_unrestricted_unknown_and_flexible_compatibility():
    assert prefs.evaluate('Unknown ZXQ', profile(''))['passes']
    assert not prefs.evaluate('Unknown ZXQ', profile('', blocked_keywords=['used']))['passes']
    assert prefs.evaluate('Unknown ZXQ', {'categories': 'electronics'})['passes']
    assert not prefs.is_strict({'category_preferences': json.dumps({'mode': 'legacy'})})
    assert prefs.evaluate(SHIRT, {'categories': 'electronics'}, {'categories': 'clothing'})['passes']


@pytest.mark.parametrize('raw', ['broken JSON', '[]', '{"mode":"typo"}',
    '{"required_keywords":{}}', '{"excluded_categories":[1]}'])
def test_corrupt_persisted_policy_holds_delivery(raw):
    result = prefs.evaluate(PHONE, {'categories': 'electronics', 'category_preferences': raw})
    assert not result['passes'] and result['code'] == 'invalid_preferences'
    assert prefs.is_strict({'category_preferences': raw})


@pytest.mark.parametrize('raw', [{'mode': 'typo'}, {'mixed': 'typo'},
    {'excluded_categories': ['all']}, {'excluded_categories': ['electrnics']},
    {'required_keywords': ['a'] * 21}, {'required_keywords': ['a' * 81]},
    {'blocked_keywords': [False]}, {'unexpected_field': True}])
def test_validation_is_explicit(raw):
    with pytest.raises(ValueError):
        prefs.parse(raw)


def test_long_restricted_post_is_held():
    assert prefs.evaluate('Phone ' + 'x' * 21000, profile())['code'] == 'analysis_limit'


def test_profile_migration_is_idempotent_and_invalid_save_atomic():
    db.init()
    creator = db.add_influencer('Independent rules', 'rules-21', categories='electronics')
    db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
    db.init()
    assert prefs.is_strict(db.get_influencer(creator))
    with pytest.raises(ValueError):
        db.update_influencer(creator, categories='clothing', category_preferences='broken JSON')
    assert db.get_influencer(creator)['categories'] == 'electronics'


def test_dispatch_boundary_holds_unwanted_posts_without_incrementing_watchdog(monkeypatch):
    db.init()
    creator = db.add_influencer('Gadgets', 'gadgets-21', categories='electronics')
    channel = db.add_channel(creator, 'telegram', '@gadgets', status='ready', categories='all')
    db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
    sent = []
    async def send(*args):
        sent.append(args)
        return 'posted'
    monkeypatch.setattr(pipeline, '_dispatch_to_channel', send)
    inf = db.get_influencer(creator)
    ch = db.list_channels(creator)[0]
    status = asyncio.run(pipeline.dispatch_to_channel(inf, ch, SHIRT))
    assert status.startswith('skipped:category_preferences:')
    assert sent == []
    assert asyncio.run(pipeline.dispatch_to_channel(inf, ch, PHONE)) == 'posted'
    assert len(sent) == 1
    con = db._connect()
    assert con.execute('SELECT COUNT(*) FROM delivery_failures WHERE channel_id=?', (channel,)).fetchone()[0] == 0
    con.close()


def test_renderer_and_mirror_bypass_cannot_escape_creator_cap(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Blocked product reached conversion/delivery')
    monkeypatch.setattr(pipeline, 'dispatch_to_channel', forbidden)
    for bypass in (False, True):
        result = asyncio.run(pipeline._render_and_deliver_destination(
            profile(), {'categories': 'all'}, SHIRT, sig='test', bypass_display_filters=bypass,
            force_unconverted=True))
        assert result.startswith('skipped:category_preferences:')


def test_two_creators_same_deal_different_preferences(monkeypatch):
    db.init()
    gadgets = db.add_influencer('Tech', 'tech-21', categories='electronics')
    fashion = db.add_influencer('Fashion', 'fashion-21', categories='clothing')
    for creator in (gadgets, fashion):
        db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
        db.add_channel(creator, 'telegram', f'@creator{creator}', status='ready', categories='all')
    sends = []
    async def send(identifier, text, **kwargs):
        sends.append(identifier)
    monkeypatch.setattr(pipeline.telegram_ops, 'post_to_channel', send)
    result = asyncio.run(pipeline.render_and_dispatch(PHONE, [gadgets, fashion]))
    assert list(result[gadgets].values()) == ['posted']
    assert list(result[fashion].values())[0].startswith('skipped:category_preferences:')
    assert sends == [f'@creator{gadgets}']


def test_hourly_highlights_do_not_repost_old_unwanted_products(monkeypatch):
    db.init()
    creator = db.add_influencer('Now tech only', 'nowtech-21', categories='electronics')
    channel = db.add_channel(creator, 'telegram', '@nowtech', status='ready')
    db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
    db.record_post(creator, channel, 'oldshirt', status='posted', deal_text=SHIRT)
    async def forbidden(*args, **kwargs):
        pytest.fail('Hourly highlight reposted an unwanted product')
    monkeypatch.setattr(pipeline, 'dispatch_to_channel', forbidden)
    result = asyncio.run(pipeline.run_hourly_loot_highlight([creator]))
    assert not result.get(creator)


@pytest.fixture
def client(monkeypatch):
    from dashboard.app import app
    db.init()
    monkeypatch.setattr(config, 'DASHBOARD_ADMIN_PASSWORD', 'safe-admin-password')
    monkeypatch.setattr(config, 'HUB_ENV', 'development')
    monkeypatch.setattr(config, 'DASHBOARD_COOKIE_SECURE', False)
    monkeypatch.setattr(app, 'testing', False)
    with app.test_client() as client:
        with client.session_transaction() as session:
            session['dashboard_authenticated'] = True
            session['dashboard_role'] = 'admin'
            session['_csrf_token'] = 'policy-token'
        yield client


def test_saving_preferences_changes_only_selected_creator(client):
    creator = db.add_influencer('Selected', 'selected-21')
    other = db.add_influencer('Untouched', 'untouched-21')
    before = db.get_influencer(other)
    response = client.post(f'/influencer/{creator}/product-preferences', data={
        '_csrf_token': 'policy-token', 'categories': ['electronics'], 'category_mode': 'strict',
        'excluded_categories': ['gaming'], 'required_keywords': 'phone, earbuds',
        'blocked_keywords': 'refurbished', 'mixed_products': 'block'})
    assert response.status_code == 302
    inf = db.get_influencer(creator)
    assert prefs.is_strict(inf) and inf['categories'] == 'electronics'
    assert prefs.parse(inf['category_preferences'])['excluded_categories'] == ['gaming']
    assert db.get_influencer(other) == before
    assert len(db.list_audit(action='product_preferences_saved')) == 1
    page = client.get(f'/guided?creator={creator}').get_data(as_text=True)
    assert 'phone, earbuds' in page and 'refurbished' in page


def test_invalid_selection_saves_nothing_and_corrupt_policy_can_be_repaired(client):
    creator = db.add_influencer('Policy repair', 'repair-21', categories='electronics')
    before = db.get_influencer(creator)
    client.post(f'/influencer/{creator}/product-preferences', data={
        '_csrf_token': 'policy-token', 'categories': ['electrnics'], 'category_mode': 'strict'})
    assert db.get_influencer(creator) == before
    con = db._connect()
    con.execute('UPDATE influencers SET category_preferences=? WHERE id=?', ('broken', creator))
    con.commit()
    con.close()
    client.post(f'/influencer/{creator}/product-preferences', data={
        '_csrf_token': 'policy-token', 'categories': ['books'], 'category_mode': 'strict'})
    assert prefs.is_strict(db.get_influencer(creator))
    assert prefs.evaluate('Paperback book', db.get_influencer(creator))['passes']


def test_draft_and_saved_preview_use_same_rules_and_are_readonly(client):
    creator = db.add_influencer('Preview', 'previewonly-21', categories='electronics')
    other = db.add_influencer('Other', 'otherpreview-21')
    own_ch = db.add_channel(creator, 'telegram', '@own_preview', categories='clothing')
    foreign_ch = db.add_channel(other, 'telegram', '@foreign_preview')
    db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
    con = db._connect()
    before = list(con.iterdump())
    headers = {'X-CSRF-Token': 'policy-token'}
    def post(payload):
        return client.post('/api/category-preview', json=payload, headers=headers)
    assert post({'text': PHONE, 'creator_id': creator}).json['preview']['passes']
    assert not post({'text': PHONE, 'creator_id': creator, 'channel_id': own_ch}).json['preview']['passes']
    assert post({'text': PHONE, 'creator_id': creator, 'channel_id': foreign_ch}).status_code == 404
    draft = post({'text': SHIRT, 'categories': 'electronics', 'preferences': {'mode': 'strict'}})
    assert not draft.json['preview']['passes']
    assert post({'text': PHONE, 'categories': 'electronics', 'preferences': []}).status_code == 400
    assert list(con.iterdump()) == before
    con.close()


def test_preference_save_requires_csrf_and_admin(client):
    creator = db.add_influencer('Protected', 'protected-21')
    path = f'/influencer/{creator}/product-preferences'
    assert client.post(path, data={'categories': 'electronics'}).status_code == 400
    with client.session_transaction() as session:
        session['dashboard_role'] = 'team'
    assert client.post(path, data={'_csrf_token': 'policy-token', 'categories': 'electronics'}).status_code == 302
    assert not db.get_influencer(creator)['category_preferences']


def test_guided_save_enables_strict_only_when_explicitly_selected(client):
    creator = db.add_influencer('Guided strict', 'guidedstrict-21')
    response = client.post('/guided/categories', data={'_csrf_token': 'policy-token', 'creator': creator,
        'categories': ['electronics'], 'category_mode': 'strict', 'allow_amazon': '1'})
    assert response.status_code == 302
    assert prefs.is_strict(db.get_influencer(creator))


def test_same_category_different_product_requests(monkeypatch):
    db.init()
    sends = []
    creators = []
    for name, keyword in [('Phones only', 'smartphone'), ('Audio only', 'earbuds')]:
        creator = db.add_influencer(name, name.split()[0].lower() + '-21', categories='electronics')
        db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict', 'required_keywords': [keyword]}))
        db.add_channel(creator, 'telegram', f'@specific{creator}', status='ready')
        creators.append(creator)
    async def send(identifier, text, **kwargs):
        sends.append(identifier)
    monkeypatch.setattr(pipeline.telegram_ops, 'post_to_channel', send)
    asyncio.run(pipeline.render_and_dispatch(PHONE, creators))
    assert sends == [f'@specific{creators[0]}']


def test_strict_mirror_respects_destination_narrowing(monkeypatch):
    from influencer_hub import whatsapp_client
    db.init()
    creator = db.add_influencer('Strict mirror', 'strictmirror-21', categories='electronics')
    db.update_influencer(creator, category_preferences=json.dumps({'mode': 'strict'}))
    main = db.add_channel(creator, 'telegram', '@strict_main', status='ready', mirror_whatsapp='1')
    target = db.add_channel(creator, 'whatsapp_channel', '120363012345678901@newsletter',
                            role='whatsapp', status='ready', categories='clothing')
    sent = []
    async def telegram(identifier, text, **kwargs):
        sent.append(identifier)
    async def forbidden(*args, **kwargs):
        pytest.fail('A strict mirror escaped its channel narrowing')
    monkeypatch.setattr(pipeline.telegram_ops, 'post_to_channel', telegram)
    monkeypatch.setattr(whatsapp_client, 'send_text', forbidden)
    result = asyncio.run(pipeline.render_and_dispatch(PHONE, [creator]))
    assert result[creator][main] == 'posted'
    assert result[creator][target].startswith('skipped:category_preferences:')
    assert sent == ['@strict_main']


def test_offer_only_channel_and_analysis_cache_isolation():
    from influencer_hub import categories
    assert prefs.evaluate('Recharge coupon code', profile(''), {'categories': 'recharge_freebies'})['passes']
    result = categories.analyse('Phone')
    result['categories'].clear()
    result['evidence']['electronics'].append('shirt')
    independent = categories.analyse('Phone')
    assert independent['categories'] == ['electronics']
    assert independent['evidence']['electronics'] == ['phone']


def test_empty_strict_selection_cannot_accidentally_enable_everything(client):
    creator = db.add_influencer('Do not broaden', 'donotbroaden-21', categories='electronics')
    before = db.get_influencer(creator)
    client.post(f'/influencer/{creator}/product-preferences', data={
        '_csrf_token': 'policy-token', 'category_mode': 'strict'})
    assert db.get_influencer(creator) == before
    client.post('/guided/categories', data={
        '_csrf_token': 'policy-token', 'creator': creator, 'category_mode': 'strict'})
    assert db.get_influencer(creator) == before
    client.post(f'/influencer/{creator}/product-preferences', data={
        '_csrf_token': 'policy-token', 'category_mode': 'strict', 'categories': ['all']})
    assert db.get_influencer(creator)['categories'] == ''
    assert prefs.is_strict(db.get_influencer(creator))


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '9' * 25, '9223372036854775808'])
def test_saved_preview_rejects_bad_identifiers(client, value):
    response = client.post('/api/category-preview', json={'text': PHONE, 'creator_id': value},
                           headers={'X-CSRF-Token': 'policy-token'})
    assert response.status_code == 400
