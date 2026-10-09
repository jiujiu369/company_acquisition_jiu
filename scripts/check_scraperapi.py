"""Server-side connection diagnostic; never prints credentials or raw response bodies."""
import json
from app.scraperapi_service import ScraperAPIService

if __name__ == "__main__":
    result = ScraperAPIService().test_connection()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["success"] else 1)
