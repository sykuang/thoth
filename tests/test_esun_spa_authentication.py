"""Full-valid SPA branch, not dashboard words or a home-init HTTP 200."""
import pytest
from tests.test_esun_password_notice import page as page
from backend.banks.esun import EsunCrawler
from backend.core.login_checkpoints import CheckpointKind, CheckpointPhase, evaluate_login_checkpoint

DASHBOARD = '<main id="layout-content" class="layout-content"><div class="cpo08003"><div class="index">我的看板</div></div></main>'


def test_full_valid_main_spa_branch_authenticates_without_legacy_labels(page):
    page.set_content(DASHBOARD)
    assert object.__new__(EsunCrawler)._logged_in(page) is True


@pytest.mark.parametrize('mutation', [
    "document.querySelector('main').classList.add('not-login')",
    "document.querySelector('.index').hidden=true",
    "document.querySelector('.cpo08003').hidden=true",
    "document.querySelector('.index').className='temp-index'",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<div class=temp-index>temp</div>')",
    "document.querySelector('.cpo08003').className='public-landing'",
    "document.querySelector('.cpo08003').innerHTML='<section><div class=index>我的看板</div></section>'",
    "document.querySelector('.cpo08003').append(document.querySelector('.index').cloneNode(true))",
    "document.body.append(document.querySelector('main').cloneNode(true))",
    "document.body.insertAdjacentHTML('beforeend','<input name=id>')",
])
def test_incomplete_hidden_ambiguous_or_login_visible_branch_fails_closed(page, mutation):
    page.set_content(DASHBOARD)
    page.evaluate(mutation)
    assert object.__new__(EsunCrawler)._logged_in(page) is False


def test_foreign_top_level_cannot_supply_spa_identity(page):
    page.goto('https://foreign.invalid/')
    page.set_content(DASHBOARD)
    assert object.__new__(EsunCrawler)._logged_in(page) is False


@pytest.mark.parametrize('origin,expected', [('https://foreign.invalid', False), ('https://ebank.esunbank.com.tw', True)])
def test_legacy_positive_text_is_owned_frame_only(page, origin, expected):
    page.context.route(origin+'/auth-fixture', lambda route: route.fulfill(
        content_type='text/html; charset=utf-8', body='登出 帳戶總覽 '+('x'*600)))
    page.set_content(f'<iframe src="{origin}/auth-fixture"></iframe>')
    page.frames[1].wait_for_load_state()
    assert object.__new__(EsunCrawler)._logged_in(page) is expected


@pytest.mark.parametrize('notice,kind', [('OTP', CheckpointKind.OTP_REQUIRED), ('未知要求', CheckpointKind.UNKNOWN_BLOCKER)])
def test_full_valid_branch_does_not_skip_settle_blockers(page, notice, kind):
    page.set_content(DASHBOARD+f'<div role="dialog">{notice}</div>')
    crawler=object.__new__(EsunCrawler)
    result=evaluate_login_checkpoint(page,bank='esun',phase=CheckpointPhase.POST_SUBMIT_SETTLE,
        rules=crawler.login_checkpoint_rules(),is_authenticated=crawler.is_authenticated,
        can_act=lambda:crawler._credential_origin_allowed(page))
    assert result.kind is kind
