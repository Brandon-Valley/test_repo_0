#!/usr/bin/env python3
import argparse, json, hashlib
from collections import defaultdict, Counter
from pathlib import Path

MARKER_ALIAS = {'BeltPiece': 'WaistPiece'}
RUNTIME_REQUEST_RULES = {
    'AnkleItem': ['AnklePiece'], 'Belt': ['BeltPiece'], 'Eyes': ['EyesPiece'], 'Face': ['FacePiece'],
    'HairBack': ['HairBackPiece'], 'HairFront': ['HairFrontPiece'], 'HeadItem': ['HeadPiece'],
    'Necklace': ['NecklacePiece'],
    'Shirt': ['ChestPiece','SleeveLeftPiece','SleeveRightPiece','SleeveLeftLowerPiece','SleeveRightLowerPiece','SleeveRightLowerUnderWristPiece','SleeveLeftLowerUnderWristPiece'],
    'Shoes': ['ShoeLeftPiece','ShoeRightPiece'],
    'Skirt': ['ShortsLeftPiece','LegLeftLowerPiece','LegLeftLowerUnderShoePiece','ShortsRightPiece','LegRightLowerPiece','LegRightLowerUnderShoePiece'],
    'Wings': ['WingsPiece'], 'WristItem': ['WristPiece'],
}
PIECE_TO_TYPE = {
    'AnklePiece':'AnkleItem','BeltPiece':'Belt','EyesPiece':'Eyes','FacePiece':'Face','HairBackPiece':'HairBack','HairFrontPiece':'HairFront',
    'HeadPiece':'HeadItem','NecklacePiece':'Necklace','ChestPiece':'Shirt','SleeveLeftPiece':'Shirt','SleeveRightPiece':'Shirt',
    'SleeveLeftLowerPiece':'Shirt','SleeveRightLowerPiece':'Shirt','SleeveRightLowerUnderWristPiece':'Shirt','SleeveLeftLowerUnderWristPiece':'Shirt',
    'ShoeLeftPiece':'Shoes','ShoeRightPiece':'Shoes','ShortsLeftPiece':'Skirt','ShortsRightPiece':'Skirt','LegLeftLowerPiece':'Skirt',
    'LegRightLowerPiece':'Skirt','LegLeftLowerUnderShoePiece':'Skirt','LegRightLowerUnderShoePiece':'Skirt','WingsPiece':'Wings','WristPiece':'WristItem'
}

def norm_path(value):
    if not value: return ''
    return value.replace('\\','/').lower().lstrip('./')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--placement',required=True); ap.add_argument('--names',required=True); ap.add_argument('--cache-map',required=True); ap.add_argument('--output',required=True); args=ap.parse_args()
    data=json.loads(Path(args.placement).read_text()); names=json.loads(Path(args.names).read_text()); cache=json.loads(Path(args.cache_map).read_text())
    name_by_id={x['item_id']:x for x in names['items']}; symbols=data['wearable_symbols']; by_source_class=defaultdict(list); by_class=defaultdict(list)
    for symbol in symbols:
        source=norm_path(symbol.get('source_swf')); cls=(symbol.get('class_name') or '').lower()
        by_source_class[(source,cls)].append(symbol); by_source_class[(Path(source).name,cls)].append(symbol); by_class[cls].append(symbol); symbol['exact_cache_links']=[]
    neutral_pose_slots=defaultdict(list)
    for pose_id,pose in data.get('poses',{}).items():
        if pose.get('pose_name') not in ('VectorModelContainer','MaleVectorModelContainer'): continue
        for slot in pose.get('slots',[]):
            neutral_pose_slots[(pose.get('pose_name'),slot.get('instance_name'))].append({'pose_id':pose_id,'slot_path':slot.get('path'),'depth_path':slot.get('depth_path'),'world_matrix':slot.get('world_matrix')})
    exact_links=[]; item_piece_map=defaultdict(list); match_counts=Counter()
    for link in cache['asset_links']:
        if link.get('item_id') is None or not link.get('piece_name'): continue
        source=norm_path(link.get('swf_url')); cls=(link.get('linkage_class') or '').lower()
        matches=by_source_class.get((source,cls),[]) or by_source_class.get((Path(source).name,cls),[]); method='source_and_linkage_class'
        if not matches:
            matches=by_class.get(cls,[]); method='linkage_class_only' if matches else 'unresolved'
        target=MARKER_ALIAS.get(link['piece_name'],link['piece_name']); placement_refs=[]
        for pose_name in ('VectorModelContainer','MaleVectorModelContainer'):
            for slot in neutral_pose_slots.get((pose_name,target),[]): placement_refs.append({'pose_name':pose_name,**slot})
        record={'cache_id':link['cache_id'],'item_id':link['item_id'],'piece_name':link['piece_name'],'target_marker_name':target,'swf_id':link.get('swf_id'),'swf_url':link.get('swf_url'),'linkage_class':link.get('linkage_class'),'matched_symbol_ids':[x['symbol_id'] for x in matches],'match_method':method,'neutral_pose_slots':placement_refs}
        exact_links.append(record); item_piece_map[link['item_id']].append(record); match_counts[method]+=1
        for symbol in matches: symbol['exact_cache_links'].append({'cache_id':link['cache_id'],'item_id':link['item_id'],'piece_name':link['piece_name'],'target_marker_name':target})
    surviving_catalog=defaultdict(list)
    for item in data.get('catalog_items',[]):
        if isinstance(item.get('item_id'),int): surviving_catalog[item['item_id']].append(item)
    item_ids=sorted(set(name_by_id)|set(item_piece_map)|set(surviving_catalog)); items=[]
    for item_id in item_ids:
        localized=name_by_id.get(item_id,{}); pieces=item_piece_map.get(item_id,[]); types=Counter(PIECE_TO_TYPE.get(x['piece_name'],'Unknown') for x in pieces); inferred=types.most_common(1)[0][0] if types else None
        items.append({'item_id':item_id,'names':localized.get('names',[]),'primary_name':(localized.get('names') or [None])[0],'descriptions':localized.get('descriptions',[]),'attributes':localized.get('attributes',[]),'localized_source_xml_files':localized.get('source_xml_files',[]),'inferred_item_type':inferred,'piece_names':sorted({x['piece_name'] for x in pieces}),'exact_piece_links':pieces,'surviving_inventory_or_wardrobe_records':surviving_catalog.get(item_id,[])})
    data['_meta']['exact_runtime_mapping']='cacheableMedia.xml maps itemId_PieceName keys to an SWF bundle and linkage class. AvatarItem requests those keys and AvatarController inserts each piece into the matching marker. exact_item_piece_links is therefore the authoritative item-to-artwork map.'
    data['_meta']['runtime_source_notes']={'piece_request_key_format':'{item_id}_{piece_name}','belt_marker_alias':'BeltPiece is inserted at the avatar marker named WaistPiece.','hair_back_special_case':'AvatarController repositions HairBackPiece using the head hairBackAnchor after assembly.','shorts_left_special_case':'ShortsLeftPiece is kept as a root-level marker and rotated with the left upper leg.','shoe_preview_only_note':'The shop preview scales and offsets the right shoe for display; in-avatar placement uses the avatar marker matrices.'}
    data['runtime_piece_request_rules']=RUNTIME_REQUEST_RULES; data['exact_item_piece_links']=exact_links; data['items_by_id']=items; data['localized_item_name_catalog']=names; data['cacheable_media_statistics']=cache['statistics']
    data['statistics'].update({'localized_item_ids':len(name_by_id),'exact_runtime_item_piece_links':len(exact_links),'exact_runtime_item_ids':len(item_piece_map),'items_by_id':len(items),'exact_link_matches_source_and_class':match_counts['source_and_linkage_class'],'exact_link_matches_class_only':match_counts['linkage_class_only'],'unresolved_exact_links':match_counts['unresolved']})
    out=Path(args.output); out.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n'); digest=hashlib.sha256(out.read_bytes()).hexdigest(); print(json.dumps(data['statistics'],indent=2)); print('bytes',out.stat().st_size,'sha256',digest)

if __name__=='__main__': main()
