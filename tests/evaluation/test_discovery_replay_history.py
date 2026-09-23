from app.evaluation.judge import compact_metadata
from app.evaluation.regression_runner import replay_history


def test_multi_turn_evaluation_preserves_question_and_cache_without_mutating_prior_run():
    question={'slot':'case_size','topic':'hamilton','adaptive':{'asked':['budget'],'candidates':[{'id':'1'}]}}
    compact=compact_metadata({'discovery_question':question,'adaptive_discovery':{'decision':'ask'}})
    turns=[{'input':'quero Hamilton','replay':{'reply':'Qual tamanho?',
             'safety_reason':'commerce_clarification','metadata':compact}}]
    history=replay_history([],turns)
    assert history[-1]['metadata']['discovery_question']==question
    assert history[-1]['metadata']['safety_reason']=='commerce_clarification'
    history[-1]['metadata']['discovery_question']['adaptive']['asked'].append('color')
    assert question['adaptive']['asked']==['budget']
