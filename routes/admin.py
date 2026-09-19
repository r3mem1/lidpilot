"""
Административная панель владельца SaaS — разделы 11 и 15 ТЗ:
    GET /admin/businesses
    GET /admin/logs
    PUT /admin/businesses/{business_id}/status

ЭТАП 6. Роутер объявлен пустым; доступ к нему будет закрыт зависимостью
require_platform_admin (раздел 15: панель только для ADMIN).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin"])
