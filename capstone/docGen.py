import argparse
import json
import time
import warnings
from collections import Counter
from datetime import date, datetime

import faiss
import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from transformers import (AutoModelForCausalLM, AutoTokenizer,
                          BitsAndBytesConfig, logging)

args = None
parser = argparse.ArgumentParser(description="Generates military documents using an LLM based on input from the user.")
parser.add_argument("-k", "--top-k", type=int, help="Specify the number of related documents to identify for context when creating the new document, default is 5.", default=5)
parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode")
parser.add_argument("-e", "--examples", action="store_true", help="Display examples used for RAG")
parser.add_argument("--cpu", action="store_true", help="Enable CPU-only mode")
parser.add_argument("-p", "--print", action="store_true", help="Print the output to the terminal when done")
parser.add_argument("-m", "--max", type=int, default=5000, help="Set the max number of input tokens, default is 5000")

warnings.filterwarnings("ignore", category=UserWarning)

EMBED_MODEL = "intfloat/e5-large-v2"

# This variable control the max length of the input to the model
MAX_INPUT_TOKENS = getattr(args, 'max', 5000)

NAV_META = "./data/NAVADMINS/metadata.json"
MAR_META = "./data/MARADMINS/metadata.json"
RTW_META = "./data/RTW/metadata.json"
OPORD_META = "./data/OPORDS/metadata.json"

NAV_INDEX = "./data/NAVADMINS/index.faiss"
MAR_INDEX = "./data/MARADMINS/index.faiss"
RTW_INDEX = "./data/RTW/index.faiss"
OPORD_INDEX = "./data/OPORDS/index.faiss"

def gen(model_num: int, type_num: int, prompt: str, save: bool = False) -> str:
    """Generates a specified document using a specified LLM and returns the result.

    :param model_num: The number value representing the model to use for generation.
    :type model_num: int
    :param type_num: The number value representing the type of document to generate.
    :type type_num: int
    :param prompt: The prompting given to the LLM to use for generation.
    :type prompt: str
    :param save: Specifies if the output should be saved to the disk or not, defaults to True
    :type save: bool, optional
    :return: The document produced by the LLM
    :rtype: str
    """
    logging.set_verbosity_error()
    model_name = select_model(model_num)
    doc_type = select_doc(type_num)

    today = date.today()
    formatted_date = today.strftime("%d %B, %Y")

    doc_instructions = ""

    match doc_type:
        case "NAVADMIN":
            doc_instructions = "The document you must write is a NAVADMIN. A NAVADMIN is a Navy Administrative Message used to disseminate information, policies, and instructions. Your response must be in exact NAVADMIN formatting. Your response must both begin and end with the CLASSIFICATION line."
        case "MARADMIN":
            doc_instructions = "The document you must write is a MARADMIN. A MARADMIN is used by Headquarters Marine Corps staff agencies and specific authorized commands to disseminate route and administrative information applicable to all Marines. You response must be in exact MARADMIN formatting."
        case "OPORD":
            doc_instructions = ("An OPORD, Operations Order, or Five Paragraph Order is used to issue an order in a clear and concise manner. There are 5 elements to this order: Situation, Mission, Execution, Administration and Logistics, and Command and Signal. You must write all 5 paragraphs.\n"
            "The situation paragraph contains information on the overall status and disposition of both friendly and enemy forces. It contains 3 subparagraphs on enemy forces and friendly forces.\n"
            "The mission paragraph provides a clear and concise statement of what the unit must accomplish. This is the heard of the order and must contain the who, what, when, where, and why of the operation.\n"
            "The execution paragraph contains the information needed to conduct the operation. It includes 3 subparagraphs on concept of operations, tasks, and coordination instructions.\n"
            "The administration and logistics paragraph contains information or instructions pertaining to rations and ammunition, location of the distribution point, corpsman, aid station, handling of prisoners of war, and other matters.\n"
            "The command and signal paragraph contains 2 subparagraphs on the chain of command with their location and signal instructions for frequencies, call signs, radio procedures, etc.")
        case "RTW":
            doc_instructions = "The document you must write is a Road to War Brief. This brief describes the scenario and narrative that sets the stage for a conflict, outlining the events and factors leading up to the conflict."
        case _:
            doc_instructions = ""

    # Set LLM instructions
    role = "Role: You work for the United States Department of Defense, and you specialize in writing official military documents using military formatting."
    task = f"{doc_instructions} Your answer must be a complete document. Do not add any additional content outside of the document. Today's date is {formatted_date}. Adjust the dates in your response accordingly. The following examples are all examples of the same type of document that you must create. Study their formatting carefully before giving your response."
    
    # create model objects
    use_cpu = getattr(args, 'cpu', False)
    if torch.cuda.is_available() and not use_cpu:
        model = AutoModelForCausalLM.from_pretrained(model_name, device_map="auto", torch_dtype="auto", quantization_config=BitsAndBytesConfig(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True), offload_folder="./offload", offload_state_dict=True)
    else:
        model = AutoModelForCausalLM.from_pretrained(model_name, device_map="cpu", torch_dtype="auto", offload_folder="./offload", offload_state_dict=True)
    
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    
    use_verbose = getattr(args, 'verbose', False)
    if use_verbose:
        device_map = model.hf_device_map
        counter = Counter(device_map.values())
        print("\nDevice map summary:")
        for device, count in counter.items():
            print(f"  {device}: {count} layers/modules")   
    
    # set up prompt info
    if doc_type == "OPORD":
        response = opord_gen([prompt, role, task], model, tokenizer)
        
        use_print = getattr(args, 'print', False)
        if use_print:
            print(f"----------Generated document----------\n{response}")
        
        return response
    else:
        examples = load_examples(doc_type, prompt)
        prompt = f"{role}\n{task}\n{examples}\nNow that you have studied the examples, create the same type of example using the following prompt: {prompt}"
        model_inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=MAX_INPUT_TOKENS).to(model.device)
        
        # generate response
        t_start = time.time()
        generated_ids = model.generate(**model_inputs, do_sample=True, max_new_tokens=1000, eos_token_id=tokenizer.eos_token_id)
        response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0][len(prompt):]
        t_stop = time.time()
        print(f"Generation time: {t_stop - t_start} sec / {(t_stop - t_start) / 60} min")
        
        if save:
            save_response(response, prompt, model_name, model)
        
        # clean up memory
        del model_inputs
        del generated_ids
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        use_print = getattr(args, 'print', False)
        if use_print:
            print(f"----------Generated document----------\n{response}")

        return response

def opord_gen(prompts: list[str], model, tokenizer):
    t_start = time.time()
    prompt = prompts[0]
    role = prompts[1]
    task = prompts[2]    
    user_input = prompt.split("[SEP]")
    all_prompt = role + task
    paragraphs = []
    
    topics = ["Orientation", "Situation", "Mission", "Execution", "Administration", "Logistics", "Command and Signal"]
    
    i = 0
    for t in topics:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        example_query = f"Write the {t} paragraph. The subject is: {user_input[i]}"
        examples = load_examples("OPORD", example_query) 
    
        p = f"{all_prompt} Write the {t} paragraph using the following examples.\n{examples} The subject is: {user_input[i]}."
        i += 1
        
        use_verbose = getattr(args, 'verbose', False)
        if use_verbose:
            print(f"[INFO] The length of the prompt is: {len(p)}")
        
        model_inputs = tokenizer(p, return_tensors="pt").to(model.device)
        generated_ids = model.generate(**model_inputs, do_sample=True, max_new_tokens=1000, eos_token_id=tokenizer.eos_token_id)
        response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0][len(prompt):]
        paragraphs.append(response)
    
        if use_verbose:
            print(f"----------{t} Paragraph----------\n{response}")
    
    response = "\n".join(paragraphs)
    t_stop = time.time()
    print(f"Generation time: {t_stop - t_start} sec / {(t_stop - t_start) / 60} min")
    return response

def find_most_rel(query: str, index, top_k: int):
    """Returns the indices of the most related documents based on the user's query

    :param query: The user's query
    :type query: str
    :param index: The FAISS index of all of the corresponding documents
    :type index: file
    :param top_k: Number of most similar results to return
    :type top_k: int
    :return: A list of the top k indices
    :rtype: list
    """
    embed_model = SentenceTransformer(EMBED_MODEL, device=None)
    query_embed = embed_model.encode([query], normalize_embeddings=True)
    distances, indices = index.search(np.array(query_embed), top_k)
    
    return indices

def load_examples(type: str, prompt: str) -> str:
    """Returns as many real life examples of the requested document type as long as total word count is under MAX_TOKENS.

    :param type: The document type
    :type type: str
    :param prompt: The prompt the user entered
    :type prompt: str
    :param prompt: The prompt the user entered
    :type prompt: str
    :return: A string of all of the examples concatenated together
    :rtype: str
    """
    # load in examples for few shot prompting
    examples = "Read the following examples very carefully. Your response must follow the same formatting as these examples.\n"
    
    index = None
    docs = None
    meta_path = ""
    
    match type:
        case "NAVADMIN":
            index = faiss.read_index(NAV_INDEX)
            meta_path = NAV_META
        case "MARADMIN":
            index = faiss.read_index(MAR_INDEX)
            meta_path = MAR_META
        case "RTW":
            index = faiss.read_index(RTW_INDEX)
            meta_path = RTW_META
        case "OPORD":
            index = faiss.read_index(OPORD_INDEX)
            meta_path = OPORD_META
    
    with open(meta_path, "r") as f:
        docs = json.load(f)
    
    top_k = getattr(args, 'top-k', 1)
    top_k_indices = find_most_rel(prompt, index, top_k)
    
    examples = ""
    for i in top_k_indices[0]:
        new_text = docs[i]['text']
        if len(examples) + len(new_text) < MAX_INPUT_TOKENS:
            examples += f"Example:\n{new_text}\n\n"
    
    use_examples = getattr(args, 'examples', False)
    if use_examples:
        print(f"---------- EXAMPLES SELECTED FOR RAG ----------\n{examples}")
        print("---------- END EXAMPLES ----------")

    return examples

def save_response(response: str, prompt: str, model_name: str, model: str):
    """Writes the response and information about how it was generated to the disk.

    :param response: The document generated by the LLM
    :type response: str
    :param prompt: The prompt given by the user to the LLM to generate the document
    :type prompt: str
    :param model_name: The LLM model used to generate the document
    :type model_name: str
    :param model: The model object
    :type model: str
    """    
    # save response to file
    fname = datetime.now().strftime("%d-%b-%Y_%H:%M:%S")
    path = f"output/{fname}.txt"
    with open(path, 'w') as file:
        file.write(f"---------- RESPONSE ----------\n{response}\n\n")
        file.write(f"---------- PROMPT ----------\n{prompt}\n\n")
        file.write(f"---------- MODEL ----------\n{model_name}\n{model}")

def select_model(num: int) -> str:
    """Translates between the numeric representation of a model to the full string of its name.

    :param num: The model number selected from the menu
    :type num: int
    :return: The full string of the model name to fetch from the Hugging Face hub
    :rtype: str
    """    
    model = ""
    
    match num:
        case 1:
            model = "meta-llama/Llama-3.2-1B-Instruct"
        case 2:
            model = "meta-llama/Llama-3.2-3B-Instruct"
        case 3:
            model = "meta-llama/Llama-3.1-8B-Instruct"
        case _:
            model = "meta-llama/Llama-3.2-3B-Instruct"

    return model

def select_doc(num: int) -> str:
    """Translates between the numeric representation of a document type and its full name.

    :param num: The document number selected from the menu
    :type num: int
    :return: The full string of the document type to generate and fetch examples
    :rtype: str
    """
    type = ""
    
    match num:
        case 1:
            type = "NAVADMIN"
        case 2:
            type = "MARADMIN"
        case 3:
            type = "OPORD"
        case 4:
            type = "RTW"
        case _:
            type = "NAVADMIN"

    return type

if __name__ == "__main__":
    args = parser.parse_args()
    
    while True:
        try:
            # model to select model you want to load
            select = int(input("Select the Llama model you would like to run\n1) Llama 3.2 1B Instruct\n2) Llama 3.2 3B Instruct\n3) Llama 3.1 8B Instruct\n4) Exit\n> "))
            
            if select < 1 or select > 4:
                print("ERROR! you did not select a correct model option.")
                continue
            elif select == 4:
                print("Exiting...")
                quit()
    
            try:
                # get document type from user
                doc = int(input("Select document to generate\n1) Naval Message (NAVADMIN)\n2) USMC Message (MARADMIN)\n3) USMC OPORD\n4) Road to War\n> "))
    
                if doc < 1 or doc > 4:
                    print("ERROR! You did not select a correct document options.")
                    continue
                if doc == 3:
                    o = "Annapolis, MD"
                    s = "We have 300 US Marines, and there are 2000 enemy fighters who have captured the US Naval Academy and taken midshipmen and faculty hostage."
                    m = "Recapture the Naval Academy and rescue all hostages."
                    e = "3 Pronged using combined arms with artillery. DDG 51 USS Arleigh Burke is available to support from the Chesapeake Bay."
                    a = "Commander is CAPT Walter Allman, USN"
                    l = "Resupply will be via helicopter at the LZ on Hospital Point"
                    c = "Make up some cool callsigns with frequencies."
                    # o = input("Orientation> ")
                    # s = input("Situation> ")
                    # m = input("Mission> ")
                    # e = input("Execution> ")
                    # a = input("Administration> ")
                    # l = input("Logistics> ")
                    # c = input("Command & Signal> ")
                    user_query = "[SEP]".join([o, s, m, e, a, l, c])
                else:
                    user_query = input("Input> ")

                # call document generator function
                doc = gen(select, doc, user_query)
            except ValueError:
                print("ERROR! Please only enter a number!")    
        except ValueError:
            print("ERROR! Please only enter a number!")    
