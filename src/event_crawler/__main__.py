from pathlib import Path
import os
import json
import time
import concurrent.futures
import tempfile

from event_crawler.search import search_and_format
from event_crawler.chat_to_json import ChatToJson
from event_crawler.crawler import WebpageToMarkdownCrawler, encode_to_markdown_text
from event_crawler.system_prompts import SP_SORT_DDG_RESULTS, SP_EXTRACT_EVENTS, SP_DEDUPLICATE_EVENTS

import datetime
import calendar

N_PROCESSES = 3
MAX_HOURS_SPENT = 3
LLM_MODEL = 'llama3.2'

# Search settings
N_MONTHS = 2
N_SEARCH_RESULTS = N_PROCESSES * 3
N_SEARCH_RETRIES = 15

N_CRAWL_PAGES = N_PROCESSES * 6


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


def _get_starting_urls(chat: ChatToJson):
    # Gets initial set of URLs based on DDG search.
    query_terms = ["evenemang", "events", "linkoping", "linköping"]
    next_months, years = _get_date_search_terms(next_n_months=N_MONTHS)
    query_terms.extend(next_months)
    query_terms.extend(years)
    search_query = " ".join(query_terms)
    print(f"{search_query=}")

    user_prompt, results = search_and_format(
        search_query,
        max_results=N_SEARCH_RESULTS,
        region='se-sv',
        timelimit='y',
        safesearch='on',
        max_retries=N_SEARCH_RETRIES
    )
    print(f'Collected {len(results)} search results.')

    output_model = {'id': None, 'relevance_tier': None, 'reasoning': None}
    result_order = chat.handle(
        user_prompt=user_prompt,
        system_prompt=SP_SORT_DDG_RESULTS,
        output_model=output_model
    )
    ordered_results = [results.get(ele['id'], None)
                       for ele in result_order]
    ordered_results = [ele for ele in ordered_results if ele is not None]
    return ordered_results


def _extract_events(chat, data_store: list[dict]):
    event_output_model = {
        "title": None,
        "description": None,
        "date": None,
        "time": None,
        "url": None
    }

    failed_pages = list()
    all_events = list()
    all_times = list()

    # Helper function to process a single page so it can be run in a thread
    def process_page(page):
        start_time = time.time()
        try:
            page_events = chat.handle(
                user_prompt=page['content'],
                system_prompt=SP_EXTRACT_EVENTS,
                output_model=event_output_model
            )
            d_time = int(time.time() - start_time)
            return page, page_events, d_time, None
        except Exception as ex:
            d_time = int(time.time() - start_time)
            return page, None, d_time, ex

    # Use ThreadPoolExecutor to run multiple requests in parallel.
    # You can specify max_workers (e.g., max_workers=4) if you want to cap the concurrency.
    with concurrent.futures.ThreadPoolExecutor(max_workers=N_PROCESSES) as executor:
        # Submit all tasks to the thread pool
        # Sort by content length, longest first; assuming that the longest have most events.
        data_store = sorted(data_store, key=lambda x: len(
            x['content']), reverse=True)
        futures = [executor.submit(process_page, page) for page in data_store]

        # as_completed yields futures as soon as they finish, regardless of the order they were submitted
        for idx, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            page, page_events, d_time, error = future.result()
            all_times.append(d_time)

            if error:
                print(error)
                failed_pages.append((page, error))
            else:
                all_events.extend(page_events)
                print(f"Extracted {len(page_events)} events ({d_time}s)")

            print(
                f'Processed {idx}/{len(data_store)} pages. Extracted {len(all_events)} events in total ({sum(all_times)}s; {sum(all_times)/idx:.2f}s/page)')

    # Deduplicate events
    user_prompt = encode_to_markdown_text(all_events)
    all_events = chat.handle(
        user_prompt=user_prompt,
        system_prompt=SP_DEDUPLICATE_EVENTS,
        output_model=event_output_model
    )

    return all_events


def _store_events(chat: ChatToJson, all_new_events: list):
    base_path = Path("data").absolute().resolve()
    os.makedirs(base_path, exist_ok=True)

    # Stores the results of all individual crawl chunks.
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, dir=base_path) as temp_file:
        print(f'Temporary output file created at: {temp_file.name}')
        temp_file.write(json.dumps(all_new_events, indent=4))

    # Merges it with all results.
    complete_output_path = Path("data").joinpath(
        "events.json").absolute().resolve()
    print(f'{complete_output_path=}')

    # Merge all known events.
    with open(complete_output_path, 'r') as input_file:
        all_known_events: list = json.loads(input_file.read())
    known_and_new_events = all_known_events + all_new_events

    with open(complete_output_path, 'w+') as output_file:
        output_file.write(json.dumps(known_and_new_events, indent=4))


def crawl_once(chat: ChatToJson, crawler: WebpageToMarkdownCrawler):
    crawler.crawl()
    all_events = _extract_events(chat, crawler.data_store)
    _store_events(chat, all_events)


def crawl_for_events():
    print(f'Searching for some events the next {MAX_HOURS_SPENT} hours...')
    max_time_spent = MAX_HOURS_SPENT * 60 * 60
    start_time = time.time()

    with ChatToJson(llm_model=LLM_MODEL, n_threads=N_PROCESSES) as chat:
        ordered_results = _get_starting_urls(chat)
        seed_urls = [res.href for res in ordered_results]
        crawler = WebpageToMarkdownCrawler(
            seed_urls=seed_urls, max_pages=N_CRAWL_PAGES)

        while time.time() - start_time < max_time_spent:
            crawl_once(chat, crawler)

    end_time = time.time()
    dtime = end_time - start_time

    spent_hours = dtime // 3600
    leftoverseconds = dtime % 3600
    spent_minutes = leftoverseconds // 60
    leftoverseconds = leftoverseconds % 60
    print(
        f'Spent a total of {spent_hours} hours, {spent_minutes} minutes, and {int(leftoverseconds)} seconds (≈{dtime/255:.2f}s/page).')


if __name__ == "__main__":
    crawl_for_events()
