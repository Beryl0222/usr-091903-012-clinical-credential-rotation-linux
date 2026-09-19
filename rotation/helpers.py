"""测试辅助：构造一套完整轮转场景。"""

from rotation.domain import RotationService
from rotation.store import EventStore

ADMIN_A = "admin-yiyuan"      # 派出方（区域医院医务处/护理部）
ADMIN_B = "admin-xianji"      # 接收方（县级机构科室）
MENTOR = "mentor-chen"
STAFF = "dr-wang"
NURSE_MENTOR = "mentor-li"
NURSE = "nurse-zhao"
PRIV = "PRIV-CONSULT"
PRIV_NURSE = "PRIV-WOUND"


def make_service(dates=("2026-01-01", "2026-03-31"), today="2026-02-01"):
    service = RotationService(
        EventStore(), clock=lambda: today)
    service.register_staff(STAFF, "王医生", "physician", ADMIN_A)
    service.register_staff(MENTOR, "陈带教", "physician", ADMIN_B)
    service.register_staff(NURSE, "赵护士", "nurse", ADMIN_A)
    service.register_staff(NURSE_MENTOR, "李带教", "nurse", ADMIN_B)
    service.register_staff("dr-backup", "备选医生", "physician", ADMIN_B)
    service.record_credential(STAFF, PRIV, "独立接诊权限",
                              "2026-12-31", ADMIN_A)
    service.record_credential(NURSE, PRIV_NURSE, "伤口护理",
                              "2026-02-15", ADMIN_A)
    service.lock_agreement({
        "roster_id": "R1",
        "agreement_no": "AGR-001",
        "staff_id": STAFF,
        "sending_org": "区域医联体总医院",
        "receiving_org": "县级人民医院",
        "practice_site": "县医院内科",
        "sending_admin": ADMIN_A,
        "receiving_admin": ADMIN_B,
        "mentor_id": MENTOR,
        "privileges": [PRIV],
        "learning_goals": ["独立接诊", "教学查房"],
        "duties": ["门诊接诊"],
        "start_date": dates[0],
        "end_date": dates[1],
    }, ADMIN_A)
    return service
