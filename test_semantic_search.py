import httpx
import time

API_URL = "http://localhost:8002"
HEADERS = {"X-API-Key": "sk_byb_greg_local"}

def run_tests():
    # Insert some distinct notes
    notes = [
        "The quick brown fox jumps over the lazy dog.",
        "Maribel and I discussed the NIMH grant budget for next year.",
        "Tim mentioned we need to order more SpikerBoxes for the upcoming workshop.",
        "I should buy some milk and eggs on the way home."
    ]
    
    print("Capturing test notes...")
    for note in notes:
        try:
            r = httpx.post(
                f"{API_URL}/v1/captures",
                json={"raw_content": note},
                headers=HEADERS
            )
            print(f"Captured: {note[:30]}... -> {r.status_code}")
        except Exception as e:
            print(f"Failed to capture: {e}")
        
    print("Waiting 5 seconds for background indexer to compute embeddings...")
    time.sleep(5)
    
    queries = [
        "financial planning for research", # Semantic match for grant budget
        "purchasing hardware for educational event", # Semantic match for SpikerBoxes
        "grocery shopping" # Semantic match for milk and eggs
    ]
    
    for q in queries:
        print(f"\n--- Searching for: '{q}' ---")
        try:
            r = httpx.get(
                f"{API_URL}/v1/search",
                params={"q": q, "limit": 1},
                headers=HEADERS
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    print(f"Top result: {results[0].get('raw_content')}")
                else:
                    print("No results found.")
            else:
                print(f"Error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"Search failed: {e}")

if __name__ == "__main__":
    run_tests()
