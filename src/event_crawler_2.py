
import itertools
from typing import Tuple
from pathlib import Path
import json
import time
import concurrent.futures
import tempfile
from dateutil.parser import parse

from event_crawler.search import do_ddg_search, DDGResult
from event_crawler.chat_to_json import ChatToJson
from event_crawler.crawler import WebpageToMarkdownCrawler
from event_crawler.system_prompts import SP_EXTRACT_EVENTS

import datetime
import calendar

N_PROCESSES = 5
MAX_HOURS_SPENT = 1/3
LLM_MODEL = 'llama3.2'

# Search settings
N_MONTHS = 2
N_SEARCH_RESULTS = N_PROCESSES * 3
N_SEARCH_RETRIES = 15

N_CRAWL_PAGES = N_PROCESSES * 6


event_output_model = ["title", "description", "date", "time"]


def _get_date_search_terms(next_n_months: int):
    # 1. Grab the current date
    current_date = datetime.date.today()
    current_month = current_date.month
    current_year = current_date.year

    # Initialize the data structures
    next_months = list()
    years = set()

    # 2. Calculate the next three months
    for i in range(0, next_n_months + 1):
        # Calculate the target month and year
        # We use 0-indexed math for the modulo operation, then add 1 back
        target_month = (current_month + i - 1) % 12 + 1

        # Calculate how many years we need to roll over
        year_offset = (current_month + i - 1) // 12
        target_year = current_year + year_offset

        # Add the month name to the list
        next_months.append(calendar.month_name[target_month])

        # Add the year to the set (duplicates are automatically ignored by sets)
        years.add(str(target_year))

    return next_months, years


def _get_starting_urls():
    urls = [
        "https://visitlinkoping.se/en/event/",
        "https://www.linkoping.se/uppleva-och-gora/evenemang-i-linkoping",
        "https://kulturportalen.linkoping.se/Schedule/Events"
    ]
    ddg_res = [DDGResult(identifier=chr(65 + i), title="Visit Linköping - Events",
                         href=url, body="") for i, url in enumerate(urls)]
    return ddg_res

    # Gets initial set of URLs based on DDG search.
    query_terms = ["evenemang", "events", "linkoping", "linköping"]
    next_months, years = _get_date_search_terms(next_n_months=N_MONTHS)
    query_terms.extend(next_months)
    query_terms.extend(years)
    search_query = " ".join(query_terms)
    print(f"{search_query=}")

    results = do_ddg_search(
        search_query,
        max_results=N_SEARCH_RESULTS,
        region='se-sv',
        timelimit='y',
        safesearch='on',
        max_retries=N_SEARCH_RETRIES
    )
    print(f'Collected {len(results)} search results.')
    return results


def _crawl_and_extract(chat: ChatToJson, crawler: WebpageToMarkdownCrawler, max_time_spent: float) -> Tuple[int, int, str]:
    start_time = time.time()
    n_pages = 0
    n_events = 0
    all_events = list()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json", prefix="events_", dir="data/tmp") as temp_file:
        filename = temp_file.name
        while time.time() - start_time < max_time_spent:
            new_entries = crawler.crawl()
            for entry in new_entries:
                spent_time = time.time() - start_time
                if spent_time >= max_time_spent:
                    break
                n_pages += 1
                print(
                    f'Spent {int(spent_time)}/{int(max_time_spent)}s, processing new page: {entry.url}')
                page_events = chat.handle(
                    user_prompt=entry.content,
                    system_prompt=SP_EXTRACT_EVENTS,
                    output_model=event_output_model
                )
                page_events = [event for event in page_events if event.get(
                    'title') != "EXAMPLE_CANARY_MEETUP_99"]
                for event in page_events:
                    event['url'] = entry.url
                    try:
                        event['date'] = parse(event['date']).strftime(
                            '%Y-%m-%d') if event.get('date') else None
                    except Exception as e:
                        print(
                            f'Failed to parse date for event: {event.get('date')}: {type(e)}')
                n_events += len(page_events)
                all_events.extend(page_events)
                temp_file.write(json.dumps(
                    all_events, ensure_ascii=False, indent=4).encode('utf-8'))

    time_spent = time.time() - start_time
    print(f"Collected {n_events} from {n_pages} pages. Saved to {filename}.")
    print(f'Spent {time_spent:.2f}s (≈{time_spent/n_pages:.2f}s/page).')
    return n_pages, n_events, filename


def crawl_and_extract(chat: ChatToJson, crawler: WebpageToMarkdownCrawler, max_time_spent: float) -> Tuple[int, int, list[dict]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=N_PROCESSES) as executor:
        futures = [executor.submit(_crawl_and_extract, chat, crawler, max_time_spent)
                   for _ in range(N_PROCESSES)]
        tot_n_pages = 0
        tot_n_events = 0
        file_names = list()
        for future in concurrent.futures.as_completed(futures):
            n_pages, n_events, file_name = future.result()
            tot_n_pages += n_pages
            tot_n_events += n_events
            file_names.append(file_name)
    all_events = list()
    for file_name in file_names:
        with open(file_name, 'r', encoding='utf-8') as f:
            events = json.load(f)
            all_events.extend(events)
    return tot_n_pages, tot_n_events, all_events


def filter_events(events: list[dict]) -> list[dict]:
    # Filters out events that are missing required fields or have empty descriptions.
    filtered_events = [
        event for event in events
        if all(event.get(field) for field in ["title", "description", "date", "time", "url"])
    ]
    filtered_events = [
        event for event in filtered_events
        if datetime.datetime.strptime(event.get('date'), '%Y-%m-%d').date() >= datetime.date.today()
    ]
    return filtered_events


def deduplicate_events(events: list[dict]) -> list[dict]:
    for event_a, event_b in itertools.product(events, repeat=2):
        if event_a is event_b:
            continue
        a = set(event_a['description'].split())
        b = set(event_b['description'].split())
        j_sim = len(a.intersection(b)) / min(len(a), len(b)
                                             ) if min(len(a), len(b)) > 0 else 0
        if j_sim > 0.8:
            print(
                f"Duplicate found: {event_a['title']} and {event_b['title']} (Jaccard similarity: {j_sim:.2f})")
            events.remove(event_b)
    return events


def main():
    max_time_spent = MAX_HOURS_SPENT * 60 * 60
    start_time = time.time()

    ordered_results = _get_starting_urls()
    seed_urls = [res.href for res in ordered_results]
    crawler = WebpageToMarkdownCrawler(seed_urls=seed_urls, max_pages=1)
    with ChatToJson(llm_model=LLM_MODEL, n_threads=N_PROCESSES) as chat:
        tot_n_pages, tot_n_events, all_events = crawl_and_extract(
            chat, crawler, max_time_spent)

    all_events = filter_events(all_events)
    all_events = deduplicate_events(all_events)

    output_path = Path("data").joinpath("events.json").absolute().resolve()
    with open(output_path, 'w+', encoding='utf-8') as output_file:
        output_file.write(json.dumps(all_events, ensure_ascii=False, indent=4))

    end_time = time.time()
    dtime = end_time - start_time

    print(f"Collected {tot_n_events} events from {tot_n_pages} pages.")

    spent_hours = dtime // 3600
    leftoverseconds = dtime % 3600
    spent_minutes = leftoverseconds // 60
    leftoverseconds = leftoverseconds % 60
    print(
        f'Spent {spent_hours}h, {spent_minutes}m, and {int(leftoverseconds)}s.')
    print(f'Avg time spent ≈{dtime/tot_n_pages:.2f}s/page.')
    print(f'Events saved to {output_path}.')


if __name__ == "__main__":
    main()
