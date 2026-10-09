from fastapi.testclient import TestClient

from app.main import create_app
from app.products import CollectionJob, Product
from app.tasks import Task, TaskStatus


def test_collection_sections_have_separate_pages_and_product_search(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collection = client.get("/")
        progress = client.get("/progress")
        saved = client.get("/products")
        assert all(response.status_code == 200 for response in (collection, progress, saved))
        assert 'id="collection-form"' in collection.text
        assert 'id="recent-collection"' not in collection.text
        assert 'id="saved-products"' not in collection.text
        assert 'id="recent-collection"' in progress.text
        assert 'id="collection-form"' not in progress.text
        assert 'id="saved-products"' in saved.text
        assert 'id="recent-collection"' not in saved.text

        with app.state.session_factory.begin() as session:
            session.add_all([
                Product(source_platform="taobao", source_product_id="1001", source_url="https://item.taobao.com/item.htm?id=1001",
                        title="蓝色收纳盒", raw_data={}, current_data={"title": "蓝色收纳盒"}),
                Product(source_platform="tmall", source_product_id="2002", source_url="https://detail.tmall.com/item.htm?id=2002",
                        title="红色储物架", raw_data={}, current_data={"title": "红色储物架"}),
            ])
        by_name = client.get("/api/collection/products", params={"q": "蓝色"}).json()
        assert by_name["total"] == 1 and by_name["items"][0]["source_product_id"] == "1001"
        by_id = client.get("/api/collection/products", params={"q": "2002", "platform": "tmall"}).json()
        assert by_id["total"] == 1 and by_id["items"][0]["title"] == "红色储物架"
        assert client.get("/api/collection/products", params={"q": "1001", "platform": "tmall"}).json()["total"] == 0

        first = client.post("/api/collection/jobs", json={"url": "https://item.taobao.com/item.htm?id=1001"}).json()["task_id"]
        child = client.post("/api/collection/jobs", json={"url": "https://detail.tmall.com/item.htm?id=2002"}).json()["task_id"]
        client.post(f"/api/collection/jobs/{first}/pause")
        filtered = client.get("/api/collection/jobs", params={"status": "CANCELED", "q": "1001"}).json()
        assert filtered["total"] == 1 and filtered["items"][0]["task"]["task_id"] == first
        assert client.get("/api/collection/jobs", params={"status": "CANCELED", "q": "2002"}).json()["total"] == 0

        shop = client.post("/api/collection/jobs", json={"url": "https://shop123.taobao.com/category.htm"}).json()["task_id"]
        with app.state.session_factory.begin() as session:
            parent_job = session.get(CollectionJob, shop)
            parent_job.result_data = {**parent_job.result_data, "child_task_ids": [child]}
            child_job = session.get(CollectionJob, child)
            child_job.result_data = {**child_job.result_data, "child_only": True, "parent_task_id": shop}
            session.get(Task, child).status = TaskStatus.RUNNING
        running = client.get("/api/collection/jobs", params={"status": "RUNNING"}).json()
        assert running["total"] == 1 and running["items"][0]["task"]["task_id"] == shop
