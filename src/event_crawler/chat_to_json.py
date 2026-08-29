from ollama import Client
from ollama import ChatResponse
import json
import subprocess
import time
import regex as re
import httpx
import os


class ChatToJson:
    def __init__(
        self,
        llm_model: str,
        n_threads: int = 1,
        timeout: int = 1200,
        max_retries_on_exception: int = 2,
        chunking: bool = True
    ):
        self._model = llm_model
        self._n_threads = n_threads
        self._max_retries_on_exception = max_retries_on_exception
        self._apply_chunking = chunking
        self._client = Client(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.end()

    def start(self) -> None:
        command = ['ollama', 'serve']
        my_env = os.environ.copy()
        my_env["OLLAMA_NUM_PARALLEL"] = str(self._n_threads)
        self._proc = subprocess.Popen(command, env=my_env)
        time.sleep(2)

    def end(self) -> None:
        self._client.close()
        self._proc.kill()
        self._proc.wait()

    def handle(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        assistant_prompt: str | None = None,
        options: dict | None = None,
        output_model: dict | list | None = None
    ) -> dict[str, str] | list[dict[str, str]]:
        if output_model is None:
            output_model = dict()
        if isinstance(output_model, list):
            output_model = {key: None for key in output_model}

        if options is None:
            options = {
                "top_p": 0.9,
                "num_predict": 1500
            }

        if self._apply_chunking:
            chunks = create_overlapping_chunks(user_prompt)
            print(f'Chunked user prompt into {len(chunks)} chunks.')
        else:
            chunks = [user_prompt]

        all_data = list()
        for user_prompt_chunk in chunks:
            messages = list()
            if system_prompt is not None:
                messages.append({
                    'role': 'system',
                    'content': system_prompt
                })
            if assistant_prompt is not None:
                messages.append({
                    'role': 'assistant',
                    'content': assistant_prompt
                })
            messages.append({
                'role': 'user',
                'content': user_prompt_chunk,
            })

            for idx in range(self._max_retries_on_exception):
                if idx > 0:
                    print(
                        f'Retrying ({idx}/{self._max_retries_on_exception})...')
                data_ = list()
                try:
                    response: ChatResponse = self._client.chat(
                        model=self._model,
                        messages=messages,
                        options=options
                    )
                except httpx.ReadTimeout as e:
                    print(f'Timeout error: {e}.')
                    continue
                j_data = response.message.content
                pattern = r'\\u(?![0-9a-fA-F]{4})'
                cleaned_json = re.sub(pattern, r'\\\\u', j_data)
                try:
                    data = json.loads(cleaned_json)
                except json.JSONDecodeError as e:
                    data = salvage_truncated_json(cleaned_json)
                    if data is None:
                        print(f'JSON decode error: {e}.')
                        continue
                # Sometimes it's silly and outputs an array of arrays...
                # So we flatten the results.
                for ele in data:
                    if isinstance(ele, list):
                        data_.extend(ele)
                    else:
                        data_.append(ele)
                try:
                    for ele in data_:
                        if not isinstance(ele, dict):
                            raise ValueError(
                                f"Expected a dictionary but got {type(ele)}: {ele}")
                        for key, value in output_model.items():
                            if key not in ele:
                                ele[key] = value
                except ValueError as e:
                    print(f'Value error: {e}.')
                    continue
                all_data.extend(data_)
                break
        return all_data


def create_overlapping_chunks(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
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


def salvage_truncated_json(broken_json_str: str) -> list[dict]:
    """
    Attempts to salvage fully completed JSON objects from a truncated JSON array string.
    Discards the partially generated object at the end.
    """
    broken_json_str = broken_json_str.strip()

    # If it doesn't even start with an array bracket, we can't parse it
    if not broken_json_str.startswith('['):
        return None

    # Step 1: Try to parse it normally in case it isn't actually broken
    try:
        return json.loads(broken_json_str)
    except json.JSONDecodeError:
        pass  # It is truncated, proceed to salvage operations

    # Step 2: Find the last closing brace of a completed object
    last_brace_index = broken_json_str.rfind('}')

    if last_brace_index == -1:
        # The LLM didn't even manage to finish a single event
        return None

    # Step 3: Slice the string to keep everything up to the last complete object,
    # and properly close the JSON array.
    salvaged_str = broken_json_str[:last_brace_index + 1] + ']'

    # Step 4: Verify the salvaged string is now valid JSON
    try:
        return json.loads(salvaged_str)
    except json.JSONDecodeError:
        # If it still fails, the internal structure is deeply corrupted
        return None
