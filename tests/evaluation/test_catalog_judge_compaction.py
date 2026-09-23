from app.evaluation.regression_judge import compact_catalog_evidence


def test_unselected_candidates_are_summarized_but_selected_sheet_preserved():
    selected={'id':'selected','name':'Relógio','description':'Hardlex '+'X'*2000,
              'images':[{'https':'https://example.com/watch.jpg'}]}
    other={'id':'other','name':'Outro','description':'Z'*9000,'images':[{'https':'https://example.com/other.jpg'}]}
    tools=[{'tool':'search_products','result':{'products':[selected,other]}},
           {'tool':'get_product','result':selected}]
    compact=compact_catalog_evidence({'products':[selected]},tools)
    sheets=list(compact['catalog_evidence'].values())
    exact=next(p for p in sheets if p['id']=='selected')
    short=next(p for p in sheets if p['id']=='other')
    assert exact['description']==selected['description'] and exact['images']==selected['images']
    assert len(short['description_excerpt'])==800
    assert short['evidence_scope']=='candidate_summary_not_full_sheet'
    assert other['description']=='Z'*9000  # Input evidence remains intact.
