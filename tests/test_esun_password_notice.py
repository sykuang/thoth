"""E.SUN optional six-month reminder; public text and component contract."""
import pytest
from patchright.sync_api import sync_playwright
from backend.banks.esun import EsunCrawler
from backend.core.login_checkpoints import CheckpointKind, CheckpointPhase, evaluate_login_checkpoint

TITLE='變更密碼提醒'
BODY='您已經有半年未變更使用者密碼了，為了網路銀行交易安全，請您立即變更。'
NOTICE=f'''<dialog open class="mib-modal-container modal-container-scroll" role="dialog">
<div style="width:32px;height:32px" class="modal-close-button mib-hover" role="button" aria-label="關閉" tabindex="0" onclick="document.body.dataset.closed++;this.closest('dialog').remove()"></div>
<div class="modal-header">{TITLE}</div><div class="modal-content">{BODY}</div>
<div class="modal-footer"><button type="button" onclick="document.body.dataset.changed++">立即變更</button></div>
</dialog>'''

@pytest.fixture
def page():
 with sync_playwright() as pw:
  browser=pw.chromium.launch(headless=True)
  try:
   context=browser.new_context(service_workers='block')
   context.route('**/*',lambda r:r.fulfill(content_type='text/html; charset=utf-8',body='<meta charset="utf-8"><body data-closed="0" data-changed="0">'+NOTICE+'</body>'))
   page=context.new_page();page.goto('https://ebank.esunbank.com.tw/')
   yield page
  finally:browser.close()

def evaluate(page,phase=CheckpointPhase.POST_SUBMIT):
 crawler=object.__new__(EsunCrawler)
 return evaluate_login_checkpoint(page,bank='esun',phase=phase,rules=crawler.login_checkpoint_rules(),is_authenticated=lambda p:False,can_act=lambda:crawler._credential_origin_allowed(page))

def test_exact_six_month_reminder_closes_only_its_close_control(page):
 result=evaluate(page)
 assert result.kind is CheckpointKind.DISMISSIBLE_NOTICE
 assert result.rule_name=='esun-six-month-password-reminder'
 assert page.locator('body').get_attribute('data-closed')=='1'
 assert page.locator('body').get_attribute('data-changed')=='0'

@pytest.mark.parametrize('mutation',[
 "document.querySelector('.modal-content').textContent='距離您上次使用者密碼變更日期已經滿一年，為了網路銀行交易安全，請您立即變更。'",
 "document.querySelector('.modal-content').append(' 請立即修改密碼')",
 "document.querySelector('.modal-content').append(' OTP')",
 "document.querySelector('.modal-content').append(' 未知的新要求')",
 "document.querySelector('.modal-close-button').remove()",
 "document.querySelector('.modal-close-button').style.display='none'",
 "document.querySelector('.modal-close-button').setAttribute('aria-label','立即變更')",
 "document.querySelector('dialog').append(document.querySelector('.modal-close-button').cloneNode(true))",
 "document.body.append(document.querySelector('dialog').cloneNode(true))",
 "document.querySelector('dialog').append(document.createElement('input'))",
 "document.querySelector('.modal-close-button').outerHTML='<button class=modal-close-button role=button aria-label=關閉>立即變更</button>'",
])
def test_changed_ambiguous_or_interactive_notice_never_clicks(page,mutation):
 page.evaluate(mutation)
 result=evaluate(page)
 assert result.kind in {CheckpointKind.UNKNOWN_BLOCKER,CheckpointKind.PASSWORD_CHANGE_REQUIRED,CheckpointKind.OTP_REQUIRED}
 assert page.locator('body').get_attribute('data-closed')=='0'
 assert page.locator('body').get_attribute('data-changed')=='0'

def test_reminder_is_not_dismissed_before_credentials(page):
 assert evaluate(page,CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER
 assert page.locator('body').get_attribute('data-closed')=='0'

def test_foreign_origin_never_clicks(page):
 page.goto('https://foreign.invalid/')
 assert evaluate(page).kind is CheckpointKind.UNKNOWN_BLOCKER
 assert page.locator('body').get_attribute('data-closed')=='0'

def test_ambiguous_close_keeps_registered_safe_diagnostic(page):
 page.locator('dialog').evaluate("el=>el.append(el.querySelector('.modal-close-button').cloneNode(true))")
 result=evaluate(page)
 assert result.kind is CheckpointKind.UNKNOWN_BLOCKER
 assert result.rule_name=='esun-six-month-password-reminder'
 assert page.locator('body').get_attribute('data-closed')=='0'

def test_shared_login_submits_once_and_dismisses_reminder_once(page,monkeypatch):
 from backend.core.creds import EsunCreds
 import json
 FORM='''<meta charset="utf-8"><form><input name="id" type="text" maxlength="10"><input name="userName" type="password" maxlength="15"><input name="pxssword" type="password" maxlength="15"><button type="button" onclick="document.body.dataset.submits++">登入</button></form><script>document.body.dataset.submits=0;</script>'''
 html=FORM+'<script>document.body.dataset.closed=0;document.body.dataset.changed=0;document.querySelector("form button").addEventListener("click",()=>{document.querySelector("form").hidden=true;document.body.insertAdjacentHTML("beforeend",'+json.dumps(NOTICE)+');document.body.append(document.createTextNode("登出 帳戶總覽 存款 信用卡 "+"x".repeat(600)));});</script>'
 page.set_content(html)
 monkeypatch.setattr(page,'wait_for_timeout',lambda ms:None)
 crawler=object.__new__(EsunCrawler);crawler.name='esun'
 crawler.creds=EsunCreds(national_id='TEST-ID',user_code='TEST-USER',password='TEST-PASS')
 assert crawler._shared_login(page) is True
 assert page.locator('body').get_attribute('data-submits')=='1'
 assert page.locator('body').get_attribute('data-closed')=='1'
 assert page.locator('body').get_attribute('data-changed')=='0'
