"""Product evidence and simple setup must agree with real category routing."""
import pytest

from influencer_hub import categories, config, db, link_router, onboarding


@pytest.mark.parametrize('text,expected,excluded', [
    ('Samsung smartphone gold colour', 'electronics', 'jewellery_accessories'),
    ('Pet shampoo 500ml', 'pet_supplies', 'daily'),
    ('Dog food 5kg', 'pet_supplies', 'daily'),
    ('Whey protein powder 1kg', 'sports', 'daily'),
    ('Notebook computer ₹25999', 'electronics', 'books_stationery'),
    ('Electric iron ₹999', 'appliances', 'electronics'),
    ('Iron supplement tablets', 'daily', 'electronics'),
    ('Smart-watch ₹1299', 'electronics', 'clothing'),
    ('Baby shampoo 200ml', 'baby', 'daily'),
    ('School bag ₹499', 'books_stationery', 'travel_luggage'),
    ('చీరలు ఆఫర్ ₹599', 'clothing', 'electronics'),
    ('బియ్యం 5kg', 'daily', 'clothing'),
])
def test_specific_product_evidence(text, expected, excluded):
    result = categories.analyse(text)
    assert expected in result['categories']
    assert excluded not in result['categories']
    assert result['evidence'][expected]
    assert link_router.matches_category_filter(text, expected)
    assert not link_router.matches_category_filter(text, excluded)


def test_url_and_footer_noise_cannot_select_category():
    text = ('Cotton saree ₹599 https://example.com/electronics/phone?tag=gaming\n'
            'Join our gaming phone jewellery channel')
    assert categories.analyse(text)['categories'] == ['clothing']
    assert not link_router.matches_category_filter(text, 'electronics')


def test_phrases_do_not_hide_other_products():
    result = categories.analyse('Pet shampoo + Samsung phone + baby lotion bundle')
    assert {'pet_supplies', 'electronics', 'baby'} <= set(result['categories'])
    assert result['multi_category']
    assert categories.preview('Lipstick with coupon code SAVE50', 'beauty')['passes']
    assert not categories.preview('Lipstick with coupon code SAVE50', 'electronics')['passes']


@pytest.mark.parametrize('alias,product', [
    ('books', 'Paperback book'), ('toys', 'Baby diapers'),
    ('jewellery', 'Gold plated necklace'), ('fashion', 'Cotton saree'),
    ('pets', 'Dog food'), ('travel', 'Cabin bag'),
])
def test_legacy_filters_and_picker_ids(alias, product):
    assert categories.preview(product, alias)['passes']
    selected = onboarding.selected_categories(categories.ALIASES[alias])
    assert selected != ['all']
    assert onboarding.category_text(selected)
    assert onboarding.category_text([categories.ALIASES[alias]])


def test_all_picker_choices_have_classifier_route():
    for key, _, _ in onboarding.CATEGORY_CHOICES:
        assert key == 'all' or categories.ALIASES.get(key, key) in link_router.CATEGORY_KEYWORDS


def test_unknown_policy_and_invalid_filters(monkeypatch):
    unknown = 'Mystery model ZXQ2049 ₹999'
    assert categories.preview(unknown, 'electronics')['passes']
    assert categories.analyse(unknown)['primary'] == 'other'
    assert categories.analyse(unknown)['scores'] == {}
    monkeypatch.setattr(config, 'CATEGORY_FILTER_STRICT_UNKNOWN', True)
    assert not categories.preview(unknown, 'electronics')['passes']
    assert categories.preview(unknown, '')['passes']
    assert categories.preview('Saree', 'electrnics')['invalid_categories'] == ['electrnics']


def test_evidence_score_counts_words_not_substrings():
    result = categories.analyse('Phone phone phone')
    assert result['evidence']['electronics'] == ['phone']
    assert link_router.get_deal_category_advanced('Phone phone phone') == {'electronics': 0.65}
    assert categories.analyse('Wholesale cartography carpet') ['categories'] == ['other']


@pytest.fixture
def client(monkeypatch):
    from dashboard.app import app
    db.init()
    monkeypatch.setattr(config, 'DASHBOARD_ADMIN_PASSWORD', 'safe-admin-password')
    monkeypatch.setattr(config, 'HUB_ENV', 'development')
    monkeypatch.setattr(config, 'DASHBOARD_COOKIE_SECURE', False)
    monkeypatch.setattr(app, 'testing', False)
    with app.test_client() as client:
        yield client


def authenticate(client, role='admin'):
    with client.session_transaction() as session:
        session['dashboard_authenticated'] = True
        session['dashboard_role'] = role
        session['_csrf_token'] = 'category-preview-test-token'


def test_preview_authenticated_csrf_readonly(client):
    authenticate(client)
    con = db._connect()
    try:
        before = list(con.iterdump())
        assert client.post('/api/category-preview', json={'text': 'Phone'}).status_code == 400
        response = client.post('/api/category-preview', json={'text': 'Phone', 'categories': 'electronics'},
                               headers={'X-CSRF-Token': 'category-preview-test-token'})
        assert response.status_code == 200
        assert response.json['preview']['passes']
        assert list(con.iterdump()) == before
    finally:
        con.close()


def test_preview_requires_auth_and_admin(client):
    with client.session_transaction() as session:
        session['_csrf_token'] = 'category-preview-test-token'
    headers = {'X-CSRF-Token': 'category-preview-test-token'}
    assert client.post('/api/category-preview', json={'text': 'Phone'}, headers=headers).status_code == 401
    authenticate(client, 'team')
    assert client.post('/api/category-preview', json={'text': 'Phone'}, headers=headers).status_code == 403


@pytest.mark.parametrize('payload', [{}, [], {'text': ''}, {'text': ['Phone']},
    {'text': 'Phone', 'categories': []}, {'text': 'x' * 20001}])
def test_preview_rejects_invalid_input(client, payload):
    authenticate(client)
    response = client.post('/api/category-preview', json=payload,
                           headers={'X-CSRF-Token': 'category-preview-test-token'})
    assert response.status_code == 400


def test_guided_page_quick_picks_and_preview(client):
    authenticate(client)
    creator = db.add_influencer('Category creator', 'category-21')
    page = client.get(f'/guided?creator={creator}').get_data(as_text=True)
    assert 'Tech & appliances' in page
    assert 'Preview these unsaved choices' in page
    for key in ('appliances', 'travel_luggage', 'gaming', 'pet_supplies'):
        assert f'value="{key}"' in page


def test_cli_preview_does_not_initialise_database(monkeypatch, capsys):
    from influencer_hub.cli import main
    def forbidden():
        pytest.fail('Read-only category check must not initialize DB')
    monkeypatch.setattr(db, 'init', forbidden)
    assert main(['category-preview', '--text', 'Pet shampoo', '--categories', 'pets']) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result['passes'] and result['primary'] == 'pet_supplies'
    assert main(['category-preview', '--text', ' ']) == 2
