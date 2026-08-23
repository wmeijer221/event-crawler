from pathlib import Path
import os
import json
import time

from event_crawler.search import search_and_format
from event_crawler.chat_to_json import ChatToJson
from event_crawler.crawler import WebpageToMarkdownCrawler, encode_to_markdown_text
from event_crawler.system_prompts import SP_SORT_DDG_RESULTS, SP_EXTRACT_EVENTS, SP_DEDUPLICATE_EVENTS


import datetime
import calendar


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
    next_months, years = _get_date_search_terms(next_n_months=2)
    query_terms.extend(next_months)
    query_terms.extend(years)
    search_query = " ".join(query_terms)
    print(f"{search_query=}")

    user_prompt, results = search_and_format(
        search_query,
        max_results=10,
        region='se-sv',
        timelimit='y',
        safesearch='on',
        max_retries=4
    )
    print(f'Collected {len(results)} search results.')
    result_order = chat.handle(
        user_prompt=user_prompt, system_prompt=SP_SORT_DDG_RESULTS)
    ordered_results = [results.get(ele['id'], None)
                       for ele in result_order]
    ordered_results = [ele for ele in ordered_results if ele is not None]
    return ordered_results


def _extract_events(chat: ChatToJson, data_store: list[dict]):
    # Extracts events.
    failed_pages = list()
    all_events = list()
    all_times = list()
    for idx, page in enumerate(data_store, start=1):
        user_prompt = page['content']
        start_time = time.time()
        try:
            page_events = chat.handle(
                user_prompt=user_prompt, system_prompt=SP_EXTRACT_EVENTS)
        except Exception as ex:
            print(ex)
            failed_pages.append((page, ex))
            continue
        end_time = time.time()
        d_time = int(end_time - start_time)
        all_times.append(d_time)

        all_events.extend(page_events)
        print(f"Extracted {len(page_events)} events ({d_time}s)")
        print(
            f'Extracted {len(all_events)} events in total ({sum(all_times)}s; {sum(all_times)/idx:.2f}s/page)')
    user_prompt = encode_to_markdown_text(all_events)
    all_events = chat.handle(
        user_prompt=user_prompt, system_prompt=SP_DEDUPLICATE_EVENTS)
    return all_events


def _store_events(chat: ChatToJson, all_new_events: list):
    output_path = Path("data").joinpath("events.json").absolute().resolve()
    print(f'{output_path=}')
    os.makedirs(output_path.parent, exist_ok=True)

    # Merge all known events.
    with open(output_path, 'r') as input_file:
        all_known_events: list = json.loads(input_file.read())
    known_and_new_events = all_new_events.extend(all_known_events)

    # Filter out old events.
    today = datetime.date.today()
    upcoming_events = [
        event for event in known_and_new_events
        if datetime.date.fromisoformat(event["date"]) >= today
    ]

    # Filter out duplicate events.
    user_prompt = encode_to_markdown_text(upcoming_events)
    all_events = chat.handle(user_prompt, system_prompt=SP_DEDUPLICATE_EVENTS)
    with open(output_path, 'w+') as output_file:
        output_file.write(json.dumps(all_events))


def crawl_for_events():
    start_time = time.time()

    with ChatToJson(model='llama3.2') as chat:
        ordered_results = _get_starting_urls(chat)

        # Crawls the web.
        seed_urls = [res.href for res in ordered_results]
        crawler = WebpageToMarkdownCrawler(seed_urls=seed_urls, max_pages=255)
        crawler.crawl()

        all_events = _extract_events(chat, crawler.data_store)
        crawler.data_store.clear()

        _store_events(chat, all_events)

    end_time = time.time()
    dtime = end_time - start_time
    print(f'Spent a total of {dtime} seconds (≈{dtime/255}s/page).')


if __name__ == "__main__":
    crawl_for_events()
