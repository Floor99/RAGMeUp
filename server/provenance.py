import os
import torch
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# This is a clever little function that attempts to compute the attribution of each document retrieved from the RAG store towards the generated answer.
# The way this function works is by getting the (self-)attention scores of each token towards every other token and then computing, for each document:
# - The attention of the consecutive sequence of tokens from the user query towards the document
# - The attention of the consecutive sequence of tokens from the document towards the user query
# - The attention of the consecutive sequence of tokens from the answer towards the document
# - The attention of the consecutive sequence of tokens from the document towards the answer
# This is computed on the full thread with the proper template applied. The four attention scores are summed and divided by the total sum of all
# weights in the attention layer, which contains eg. other documents' attention but also query-query, query-answer, answer-query and query-query attention.
#
# This is by no means a foolproof way to attribute towards each of the documents properly but it is _a_ way.
def compute_attention(model, tokenizer, thread, query, context, answer):
    include_query=True
    if os.getenv("attribute_include_query") == "False":
        include_query=False

    # Encode the full thread
    thread_tokens = tokenizer.encode(thread, return_tensors="pt", add_special_tokens=False)

    # Compute the attention
    with torch.no_grad():
        output = model(input_ids=thread_tokens, output_attentions=True)
    
    # Use the last layer's attention
    attentions = output.attentions[-1]
    # Tokenize query, context parts, and answer
    query_tokens = tokenizer.encode(query, add_special_tokens=False)
    answer_tokens = tokenizer.encode(answer, add_special_tokens=False)

    # Find the start and end positions of query, context parts, and answer in the thread tokens
    query_start, query_end = find_sublist_positions(thread_tokens[0].tolist(), query_tokens)
    answer_start, answer_end = find_sublist_positions(thread_tokens[0].tolist(), answer_tokens)
    
    # Get the token offsets for each of the documents in the context
    context_offsets = []
    for part in context:
        part_tokens = tokenizer.encode(part, add_special_tokens=False)
        context_offsets.append(find_sublist_positions(thread_tokens[0].tolist(), part_tokens))
    
    # Get the total sum of the self-attentions for the given input
    total_attention_sum = attentions[0].sum().item()

    # Extract the attention weights for each document
    doc_attentions = []
    for start, end in context_offsets:
        query_to_doc_attention = 0
        
        # Focus on the attention from the answer to this document part
        answer_to_doc_attention = attentions[0, :, answer_start:answer_end, start:end].sum().item()
        doc_to_answer_attention = attentions[0, :, start:end, answer_start:answer_end].sum().item()
        if include_query:
            # Also consider the attention from the query to this document part
            query_to_doc_attention = attentions[0, :, query_start:query_end, start:end].sum().item()
            doc_to_query_attention = attentions[0, :, start:end, query_start:query_end].sum().item()
        
            # Combine these attention scores and divide them by the total of all the attention scores to get a normalized score
            doc_attention_sum = (
                query_to_doc_attention +
                answer_to_doc_attention +
                doc_to_query_attention +
                doc_to_answer_attention
            )
        else:
            doc_attention_sum = (
                answer_to_doc_attention +
                doc_to_answer_attention
            )
        normalized_attention = doc_attention_sum / total_attention_sum if total_attention_sum > 0 else 0
        doc_attentions.append(normalized_attention)

    return doc_attentions

def find_sublist_positions(thread_tokens, part_tokens):
    len_thread = len(thread_tokens)
    len_part = len(part_tokens)

    for i in range(len_thread - len_part + 1):
        if thread_tokens[i:i + len_part] == part_tokens:
            return i, i + len_part - 1
    
    raise ValueError("Sublist not found")

class DocumentSimilarityAttribution:
    def __init__(self):
        device = 'cuda'
        if os.getenv('force_cpu') == "True":
            device = 'cpu'
        self.model = SentenceTransformer(os.getenv('attribute_llm'), device=device)

    def compute_similarity(self, query, context, answer):
        include_query=True
        if os.getenv("attribute_include_query") == "False":
            include_query=False
        # Encode the answer, query, and context documents
        answer_embedding = self.model.encode([answer])[0]
        context_embeddings = self.model.encode(context)
        
        if include_query:
            query_embedding = self.model.encode([query])[0]

        # Compute similarity scores
        similarity_scores = []
        for i, doc_embedding in enumerate(context_embeddings):
            # Similarity between document and answer
            doc_answer_similarity = cosine_similarity([doc_embedding], [answer_embedding])[0][0]
            
            if include_query:
                # Similarity between document and query
                doc_query_similarity = cosine_similarity([doc_embedding], [query_embedding])[0][0]
                # Average of answer and query similarities
                similarity_score = (doc_answer_similarity + doc_query_similarity) / 2
            else:
                similarity_score = doc_answer_similarity

            similarity_scores.append(similarity_score)

        # Normalize scores
        total_similarity = sum(similarity_scores)
        normalized_scores = [score / total_similarity for score in similarity_scores] if total_similarity > 0 else similarity_scores

        return normalized_scores