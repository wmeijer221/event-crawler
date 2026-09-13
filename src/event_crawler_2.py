
import itertools

from typing import Tuple
from pathlib import Path
import json
import time
import concurrent.futures
import tempfile
from dateutil.parser import parse

from event_crawler.search import do_ddg_search, DDGResult
from event_crawler.chat_to_json import LazyJsonOllama
from event_crawler.llm.lazy_ollama import LazyOllamaChat, LazyAgenticOllamaChats
from event_crawler.llm.interfaces import OllamaModelOptions
from event_crawler.crawler import WebpageToMarkdownCrawler, CrawlEntry
from event_crawler.system_prompts import SP_SCANNER, SP_PLANNER, SP_EXTRACTER, UPT_SCANNER, UPT_PLANNER, UPT_EXTRACTER

import datetime
import calendar

import nltk
nltk.download('punkt') # Run once
nltk.download('punkt_tab') # Run once
from nltk.tokenize import sent_tokenize


N_PROCESSES = 8
MAX_HOURS_SPENT = 1/2
LLM_MODEL = 'llama3.2'
MAX_JSON_RETRIES = 2

# Search settings
N_MONTHS = 2
N_SEARCH_RESULTS = N_PROCESSES * 3
N_SEARCH_RETRIES = 15

N_CRAWL_PAGES = N_PROCESSES * 6

DATETIME_FORMAT = '%Y-%m-%d'


event_output_model = ["reasoning", "title", "description", "location", "date", "time"]


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


def _crawl_and_extract(crawler: WebpageToMarkdownCrawler, max_time_spent: float) -> Tuple[int, int, str]:
    start_time = time.monotonic()
    n_pages = 0
    n_events = 0
    all_events = list()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json", prefix="events_", dir="data/tmp") as temp_file:
        filename = temp_file.name
        while time.monotonic() - start_time < max_time_spent:
            new_entries = crawler.crawl()
            for entry in new_entries:
                spent_time = time.monotonic() - start_time
                if spent_time >= max_time_spent:
                    break
                n_pages += 1
                print(
                    f'Spent {int(spent_time)}/{int(max_time_spent)}s, processing new page: {entry.url}')

                page_events = _crawl_and_extract_once(entry)

                n_events += len(page_events)
                all_events.extend(page_events)
                # Write to file.
                temp_file.seek(0)
                j_data = json.dumps(
                    all_events, ensure_ascii=False, indent=4).encode('utf-8')
                temp_file.write(j_data)
                temp_file.flush()

    time_spent = time.monotonic() - start_time
    print(f"Collected {n_events} from {n_pages} pages. Saved to {filename}.")
    print(f'Spent {time_spent:.2f}s (≈{time_spent/n_pages:.2f}s/page).')
    return n_pages, n_events, filename


def _crawl_and_extract_once(entry: CrawlEntry) -> list[dict]:
    # Agentic architecture
    scanner_options = OllamaModelOptions(num_ctx=20_000, format='json')
    scanner_chat = LazyOllamaChat(model='llama3.2', options=scanner_options)
    scanner_chat = LazyJsonOllama(
        chat=scanner_chat, max_retries=MAX_JSON_RETRIES)
    scanner_output_model = ["reasoning", "contains_event"]

    planner_options = OllamaModelOptions(num_ctx=20_000, format='json')
    planner_chat = LazyOllamaChat(model='llama3.2', options=planner_options)
    planner_chat = LazyJsonOllama(
        chat=planner_chat, max_retries=MAX_JSON_RETRIES)
    planner_output_model = ["events"]

    extracter_options = OllamaModelOptions(num_ctx=20_000, format='json')
    extracter_chat = LazyOllamaChat(
        model='llama3.2', options=extracter_options)
    extracter_chat = LazyJsonOllama(
        chat=extracter_chat, max_retries=MAX_JSON_RETRIES)

    models_and_options = {
        'scanner': scanner_chat,
        'planner': planner_chat,
        'extracter': extracter_chat,
    }

    chat = LazyAgenticOllamaChats(
        models_and_options=models_and_options, ollama_dir=None)
    all_events = list()

    with chat:
        chunks = __create_overlapping_chunks(entry.content)
        for content_chunk in chunks:
            # AGENT 1: Scanner
            up_scanner = UPT_SCANNER.format(webpage_chunk=content_chunk)
            scanner_results = chat.chat(agent_id='scanner', user_message=up_scanner,
                                        system_message=SP_SCANNER, output_model=scanner_output_model)
            if scanner_results.get('contains_event', False) is False:
                continue

            # AGENT 2: Planner
            split_content = sent_tokenize(content_chunk)
            numbered_content_chunk = "\n".join(
                [f"[{i}] {line}" for i, line in enumerate(split_content, start=1)])
            up_planner = UPT_PLANNER.format(
                webpage_chunk=numbered_content_chunk)
            planner_results = chat.chat(agent_id='planner', user_message=up_planner,
                                        system_message=SP_PLANNER, output_model=planner_output_model)
            target_events = planner_results['events']
            if target_events is None or len(target_events) == 0:
                continue

            # AGENT 3: Extracter
            for event in target_events:
                if event.get('start_line') is None or event.get('end_line') is None:
                    continue
                start_line = event['start_line']
                end_line = event['end_line']
                start_line_ = max(0, start_line - 2)
                end_line_ = min(len(split_content), end_line + 2)
                snippet_lines = split_content[start_line_:end_line_]
                event_snippet = "\n".join(snippet_lines)
                up_extracter = UPT_EXTRACTER.format(
                    reasoning=event.get('reasoning', 'no reason specified'),
                    event_snippet=event_snippet)
                event = chat.chat(agent_id='extracter', user_message=up_extracter,
                                  system_message=SP_EXTRACTER, output_model=event_output_model)
                all_events.append(event)

    # Clean up dates.
    for event in all_events:
        event['url'] = entry.url
        try:
            event['date'] = parse(event['date']).strftime(DATETIME_FORMAT) \
                if event.get('date') else None
        except Exception as e:
            pass
    return all_events


def __create_overlapping_chunks(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """
    Splits a long string into overlapping chunks based on word count.

    :param text: The full text to be chunked.
    :param chunk_size: Maximum number of words per chunk.
    :param overlap: Number of words to overlap between chunks.
    :return: A list of chunked strings.
    """
    words = text.split()
    chunks = []

    # Prevent infinite loops if overlap is configured incorrectly
    if overlap >= chunk_size:
        raise ValueError("Overlap must be smaller than the chunk size.")

    # The step determines how far forward we jump for the next chunk
    step = chunk_size - overlap

    for i in range(0, len(words), step):
        # Slice the list of words from the current index to the chunk limit
        chunk_words = words[i:i + chunk_size]

        # Rejoin the words into a single string and add to our list
        chunks.append(" ".join(chunk_words))

    return chunks


def crawl_and_extract(chat: LazyOllamaChat, crawler: WebpageToMarkdownCrawler, max_time_spent: float) -> Tuple[int, int, list[dict]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=N_PROCESSES) as executor:
        futures = [executor.submit(_crawl_and_extract, crawler, max_time_spent)
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
    # filtered_events = [
    #     event for event in filtered_events
    #     if datetime.datetime.strptime(event.get('date'), '%Y-%m-%d').date() >= datetime.date.today()
    # ]
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
    with LazyOllamaChat(ollama_dir=None, model=LLM_MODEL, n_threads=N_PROCESSES) as chat:
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
