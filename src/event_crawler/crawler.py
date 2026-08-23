import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from collections import deque
import time
from markdownify import markdownify as md


class WebpageToMarkdownCrawler:
    def __init__(self, seed_urls, max_pages=15):
        # 1. Initialize the URL Frontier
        self.frontier = deque(seed_urls)
        # 2. Track visited URLs to prevent duplicates and loops
        self.visited = set(seed_urls)
        self.max_pages = max_pages
        self.data_store = list()

    def crawl(self):
        pages_crawled = 0

        while self.frontier and pages_crawled < self.max_pages:
            # Grab the next URL from the front of the queue
            current_url: str = self.frontier.popleft()
            if "ical.php" in current_url:
                continue

            try:
                # 3. Fetch the Content
                response = requests.get(current_url, timeout=5)

                if response.status_code == 200:
                    print(
                        f"[{pages_crawled + 1}] Successfully fetched: {current_url}")

                    # 4. Parse and Extract
                    soup = BeautifulSoup(response.text, 'html.parser')

                    # Extract target data (e.g., page title)
                    page_title = soup.title.string.strip() if soup.title else "No Title"

                    page_markdown = ""
                    # Target only the body of the webpage
                    body = soup.find('body')

                    if body:
                        # Remove structural elements that typically contain headers/footers/nav
                        unwanted_tags = ['script', 'style',
                                         'header', 'footer', 'nav', 'aside']
                        for element in body(unwanted_tags):
                            element.extract()

                        # Convert the cleaned body HTML directly to Markdown
                        # This preserves links in the format: [Link Text](http://...)
                        page_markdown = md(
                            str(body), heading_style="ATX").strip()

                    self.data_store.append(
                        {"url": current_url, "title": page_title, "content": page_markdown})

                    # Extract outbound links for the crawler frontier
                    for link in soup.find_all('a', href=True):
                        absolute_link = urljoin(current_url, link['href'])

                        # 5. Update the Frontier
                        if absolute_link.startswith('http') and absolute_link not in self.visited:
                            self.visited.add(absolute_link)
                            self.frontier.append(absolute_link)

            except requests.RequestException as e:
                print(f"Failed to fetch {current_url}: {e}")

            time.sleep(1)
            pages_crawled += 1


def encode_to_markdown_text(events: list[dict]) -> str:
    """
    Encodes a list of event dictionaries into a readable text format for an LLM.
    """
    if not events:
        return "No events provided."

    encoded_text = "Here is the list of events:\n\n"

    for i, event in enumerate(events, start=1):
        encoded_text += f"### Event {i}\n"
        encoded_text += f"* Title: {event.get('title')}\n"
        encoded_text += f"* Description: {event.get('description')}\n"

        # Handle potential null/None values gracefully
        date = event.get('date')
        encoded_text += f"* Date: {date if date else 'Not specified'}\n"

        time = event.get('time')
        encoded_text += f"* Time: {time if time else 'Not specified'}\n"

        url = event.get('url')
        encoded_text += f"* URL: {url if url else 'Not specified'}\n"

        encoded_text += "\n"

    return encoded_text.strip()
