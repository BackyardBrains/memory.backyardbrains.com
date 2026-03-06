import httpx
import time

API_URL = "http://localhost:8002"
HEADERS = {"X-API-Key": "sk_byb_greg_local"}

def run_tests():
    print("Capturing a note...")
    r = httpx.post(
        f"{API_URL}/v1/captures",
        json={"raw_content": "We need to schedule a meeting with Maribel about the NIMH grant budget."},
        headers=HEADERS
    )
    print("Capture response:", r.status_code, r.json())
    
    print("Waiting 3 seconds for background indexer...")
    time.sleep(3)
    
    print("Searching memory for 'budget discussion'...")
    r = httpx.get(
        f"{API_URL}/v1/search",
        params={"q": "budget discussion", "limit": 2},
        headers=HEADERS
    )
    print("Search response:", r.status_code)
    for res in r.json():
        print(f" - {res}")

if __name__ == "__main__":
    run_tests()
