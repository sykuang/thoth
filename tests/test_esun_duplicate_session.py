"""SPA duplicate-session confirmation uses the bank's exact public contract."""
import pytest
from tests.test_esun_password_notice import page as page, evaluate
from backend.core.login_checkpoints import CheckpointKind

DUPLICATE = '''<dialog open class="mib-modal-container" role="dialog">
<div class="modal-header">重複登入提醒</div>
<div class="modal-content">若要在此處登入，請按下「確定登入」，同時其它位置將會自動登出。</div>
<div class="modal-footer"><button type="button">取消</button>
<button type="button" onclick="document.body.dataset.confirmed++;this.closest('dialog').remove()">確定登入</button></div>
</dialog>'''

@pytest.mark.parametrize('extra, expected', [('', CheckpointKind.DUPLICATE_SESSION),
    (' OTP', CheckpointKind.OTP_REQUIRED), (' 未知要求', CheckpointKind.UNKNOWN_BLOCKER)])
def test_exact_duplicate_session_native_confirm_only(page, extra, expected):
    page.set_content('<body data-confirmed="0">'+DUPLICATE+'</body>')
    if extra:
        page.locator('.modal-content').evaluate('(n,s)=>n.append(s)', extra)
    result = evaluate(page)
    assert result.kind is expected
    assert page.locator('body').get_attribute('data-confirmed') == ('1' if not extra else '0')
