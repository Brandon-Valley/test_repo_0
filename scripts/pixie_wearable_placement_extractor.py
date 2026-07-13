#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, math, re, sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

TWIPS_PER_PIXEL = 20.0
SLOT_WORDS = {
    'ChestPiece','SleeveLeftPiece','SleeveRightPiece','SleeveLeftLowerPiece','SleeveRightLowerPiece',
    'SkirtLeftPiece','SkirtRightPiece','SkirtBackPiece','PantLeftPiece','PantRightPiece',
    'ShortsLeftPiece','ShortsRightPiece','ShoeLeftPiece','ShoeRightPiece','HeadPiece','HairFrontPiece',
    'HairBackPiece','WingsPiece','NecklacePiece','WaistPiece','WristPiece','AnklePiece','FacePiece','EyesPiece'
}
BODY_WORDS = {'VectorModelContainer','Chest','Head','UpArmLeft','LowArmLeft','HandLeft','UpArmRight','LowArmRight','HandRight','UpLegLeft','LowLegLeft','FootLeft','UpLegRight','LowLegRight','FootRight'}

@dataclass(frozen=True)
class Mat:
    a: float=1.0; b: float=0.0; c: float=0.0; d: float=1.0; tx: float=0.0; ty: float=0.0
    def mul(self, o:'Mat')->'Mat':
        return Mat(self.a*o.a+self.c*o.b,self.b*o.a+self.d*o.b,self.a*o.c+self.c*o.d,self.b*o.c+self.d*o.d,self.a*o.tx+self.c*o.ty+self.tx,self.b*o.tx+self.d*o.ty+self.ty)
    def point(self,x:float,y:float): return self.a*x+self.c*y+self.tx,self.b*x+self.d*y+self.ty
    def json_twips(self):
        det=self.a*self.d-self.b*self.c
        return {'a':rnd(self.a),'b':rnd(self.b),'c':rnd(self.c),'d':rnd(self.d),'tx_twips':rnd(self.tx),'ty_twips':rnd(self.ty),'tx_px':rnd(self.tx/20),'ty_px':rnd(self.ty/20),'rotation_deg':rnd(math.degrees(math.atan2(self.b,self.a))),'scale_x':rnd(math.hypot(self.a,self.b)),'scale_y':rnd(math.hypot(self.c,self.d)),'determinant':rnd(det),'mirrored':det<0}

def rnd(v): return round(float(v),6)
def mat_from(el):
    if el is None:return Mat()
    return Mat(float(el.get('scaleX','1')),float(el.get('rotateSkew0','0')),float(el.get('rotateSkew1','0')),float(el.get('scaleY','1')),float(el.get('translateX','0')),float(el.get('translateY','0')))
def rect_json(box):
    if box is None:return None
    x0,y0,x1,y1=box
    return {'xmin_twips':rnd(x0),'ymin_twips':rnd(y0),'xmax_twips':rnd(x1),'ymax_twips':rnd(y1),'xmin_px':rnd(x0/20),'ymin_px':rnd(y0/20),'xmax_px':rnd(x1/20),'ymax_px':rnd(y1/20),'width_px':rnd((x1-x0)/20),'height_px':rnd((y1-y0)/20),'trimmed_png_origin_px':{'x':rnd(-x0/20),'y':rnd(-y0/20)}}
def union(boxes):
    boxes=[x for x in boxes if x]
    if not boxes:return None
    return min(x[0] for x in boxes),min(x[1] for x in boxes),max(x[2] for x in boxes),max(x[3] for x in boxes)
def transform_box(box,m):
    if box is None:return None
    pts=[m.point(box[0],box[1]),m.point(box[2],box[1]),m.point(box[0],box[3]),m.point(box[2],box[3])]
    return min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)
def char_id(tag):
    for key in ('spriteId','shapeId','characterId','bitmapId','soundId','fontId','textId','buttonId','morphShapeId','videoId'):
        if key in tag.attrib and tag.attrib.get('type','').startswith('Define'):
            try:return int(tag.attrib[key])
            except:return None
    return None
def first_frame_places(defn):
    sub=defn.find('subTags') if defn is not None else None
    if sub is None:return []
    depths={}
    for t in sub:
        typ=t.get('type','')
        if typ=='ShowFrameTag':break
        if typ.startswith('RemoveObject'):
            depths.pop(int(t.get('depth','-1')),None);continue
        if typ.startswith('PlaceObject'):
            depth=int(t.get('depth','0'));prev=depths.get(depth,{}) if t.get('placeFlagMove')=='true' else {};rec=dict(prev)
            if t.get('characterId') is not None:rec['character_id']=int(t.get('characterId'))
            if t.get('name') is not None:rec['name']=t.get('name')
            if t.find('matrix') is not None:rec['matrix']=mat_from(t.find('matrix'))
            rec.setdefault('matrix',Mat());rec['depth']=depth;rec['tag_type']=typ;depths[depth]=rec
    return [depths[k] for k in sorted(depths)]

class SwfDoc:
    def __init__(self,path:Path,source_swf:str):
        self.path=path;self.source_swf=source_swf;root=ET.parse(path).getroot();tags=root.find('tags');self.defs={};self.symbols=[]
        if tags is None:return
        for t in tags:
            cid=char_id(t)
            if cid is not None and cid not in self.defs:self.defs[cid]=t
            if t.get('type')=='SymbolClassTag':
                ts=t.find('tags');ns=t.find('names');ids=[int(x.text) for x in list(ts or []) if x.text];names=[x.text or '' for x in list(ns or [])]
                self.symbols.extend(zip(ids,names))
        self._bounds={}
    def bounds(self,cid,stack=()):
        if cid in self._bounds:return self._bounds[cid]
        if cid in stack:return None
        d=self.defs.get(cid)
        if d is None:return None
        for key in ('shapeBounds','edgeBounds','bounds','characterBounds'):
            q=d.find(key)
            if q is not None and all(k in q.attrib for k in ('Xmin','Ymin','Xmax','Ymax')):
                box=float(q.get('Xmin')),float(q.get('Ymin')),float(q.get('Xmax')),float(q.get('Ymax'));self._bounds[cid]=box;return box
        box=union(transform_box(self.bounds(p['character_id'],stack+(cid,)),p['matrix']) for p in first_frame_places(d) if 'character_id' in p)
        self._bounds[cid]=box;return box
    def display_list(self,cid):
        out=[]
        for p in first_frame_places(self.defs.get(cid)):
            b=self.bounds(p.get('character_id')) if p.get('character_id') is not None else None
            out.append({'depth':p['depth'],'name':p.get('name'),'character_id':p.get('character_id'),'matrix':p['matrix'].json_twips(),'child_bounds':rect_json(b),'placed_bounds':rect_json(transform_box(b,p['matrix']))})
        return out
    def slot_paths(self,cid):
        out=[]
        def walk(cur,world,path,depths,seen):
            if cur in seen:return
            for p in first_frame_places(self.defs.get(cur)):
                if 'character_id' not in p:continue
                w=world.mul(p['matrix']);name=p.get('name');np=path+([name] if name else [f'character_{p["character_id"]}'])
                if name and (name in SLOT_WORDS or name in BODY_WORDS or name.endswith('Piece')):
                    out.append({'path':'/'.join(np),'instance_name':name,'character_id':p['character_id'],'depth_path':depths+[p['depth']],'world_matrix':w.json_twips(),'local_matrix':p['matrix'].json_twips()})
                walk(p['character_id'],w,np,depths+[p['depth']],seen+(cur,))
        walk(cid,Mat(),[],[],());return out

def classify(name):
    low=re.sub(r'[^a-z0-9]+','',name.lower())
    rules=[('hair_back',['hairback','backhair','hairbk']),('hair_front',['hairfront','fronthair']),('wings',['wing']),('necklace',['necklace','neckitem','scarf']),('waist',['waist','belt','sash']),('wrist',['wrist','bracelet']),('ankle',['ankle','anklet']),('head',['headitem','headpiece','hat','tiara','crown','bonnet','cap']),('shoe',['shoe','boot','slipper','sandal']),('sleeve_left_lower',['sleeveleftlower','leftsleevelower','ltarmlow','lalarmlow']),('sleeve_right_lower',['sleeverightlower','rightsleevelower','rtarmlow','raarmlow']),('sleeve_left',['sleeveleft','leftsleeve','ltsleeve','larmup','laarmup']),('sleeve_right',['sleeveright','rightsleeve','rtsleeve','rarmup','raarmup']),('skirt_back',['skirtback','backskirt']),('skirt_left',['skirtleft','leftskirt']),('skirt_right',['skirtright','rightskirt']),('skirt',['skirt','dressbottom']),('leg_left_lower',['legleftlower','leftleglower','lleglow','lll','leg1left']),('leg_right_lower',['legrightlower','rightleglower','rleglow','lrl','leg1right']),('leg_left_upper',['legleftupper','leftlegupper','llegup','lallegup','pantleft','shortsleft']),('leg_right_upper',['legrightupper','rightlegupper','rlegup','rallegup','pantright','shortsright']),('chest',['chest','shirt','top','bodice','vest','tunic']),('face',['face']),('eyes',['eyes']),('hair_front',['hair'])]
    for category,tokens in rules:
        for token in tokens:
            if token in low:return category,0.95,token
    return 'unclassified',0.2,None

def group_key(name):
    x=re.sub(r'(?i)(chest|shirt|top|bodice|vest|tunic|sleeveleftlower|sleeverightlower|sleeveleft|sleeveright|leftsleeve|rightsleeve|skirtleft|skirtright|skirtback|skirt|pantleft|pantright|shortsleft|shortsright|leg1left|leg1right|lleglow|rleglow|llegup|rlegup|shoeleft|shoeright|shoe|boot|slipper|headitem|headpiece|hat|necklace|scarf|waist|belt|sash|wrist|bracelet|ankle|anklet|hairfront|hairback|wings?)','_',name)
    x=re.sub(r'(?i)(left|right|lower|upper|front|back|color\d*|colour\d*)','_',x)
    return re.sub(r'[^A-Za-z0-9]+','_',x).strip('_').lower() or name.lower()

def parse_items(source_root:Path):
    designs={}
    for path in source_root.rglob('*.xml'):
        try:root=ET.parse(path).getroot()
        except:continue
        for item in root.iter('item'):
            pieces=[]
            for piece in item.findall('piece'):
                asset=piece.find('asset');frame=piece.findtext('frame')
                pieces.append({'piece_type':piece.get('type'),'asset_path':(asset.text or '').strip() if asset is not None else None,'asset_id':int(asset.get('asset_id')) if asset is not None and asset.get('asset_id','').isdigit() else None,'frame':int(frame) if frame and frame.strip().lstrip('-').isdigit() else None})
            if not pieces:continue
            key=(item.get('item_id'),item.get('name'),item.get('type'),json.dumps(pieces,sort_keys=True))
            design=designs.setdefault(key,{'item_id':int(item.get('item_id')) if (item.get('item_id') or '').isdigit() else item.get('item_id'),'name':item.get('name'),'item_type':item.get('type'),'pieces':pieces,'color_variants':[],'inventory_ids':[],'source_xml_files':set()})
            design['source_xml_files'].add(path.relative_to(source_root).as_posix());inventory=item.findtext('inventoryId')
            if inventory and inventory.strip().isdigit():design['inventory_ids'].append(int(inventory))
            colors=[]
            for color in item.findall('color'):
                cid=color.get('color_id');colors.append({'number':color.get('number'),'color_id':int(cid) if cid and cid.isdigit() else cid,'rgb':[int(x) for x in (color.text or '').split() if x.lstrip('-').isdigit()]})
            if colors and colors not in design['color_variants']:design['color_variants'].append(colors)
    out=[]
    for design in designs.values():
        design['source_xml_files']=sorted(design['source_xml_files']);design['inventory_ids']=sorted(set(design['inventory_ids']));out.append(design)
    return sorted(out,key=lambda x:(str(x['item_type']),str(x['item_id']),str(x['name'])))

PREFIX={'ChestPiece':'Chest','SleeveLeftPiece':'SleeveLeft','SleeveRightPiece':'SleeveRight','SkirtLeftPiece':'SkirtLeft','SkirtRightPiece':'SkirtRight','SkirtBackPiece':'SkirtBack','PantLeftPiece':'PantLeft','PantRightPiece':'PantRight','ShoeLeftPiece':'ShoeLeft','ShoeRightPiece':'ShoeRight','HeadPiece':'HeadItem','WristPiece':'Bracelet','AnklePiece':'Anklet','WaistPiece':'Belt','NecklacePiece':'Necklace','HairFrontPiece':'Hair','HairBackPiece':'Back','WingsPiece':'Wings','FacePiece':'Face','EyesPiece':'Eyes'}
ACCESSORY={'HeadPiece','WristPiece','AnklePiece','WaistPiece','NecklacePiece'}
def resolve_piece(item,piece,class_index):
    match=re.search(r'(\d+)$',item.get('name') or '');suffix=int(match.group(1)) if match else None;frame=piece.get('frame')
    if suffix is None and frame is not None:suffix=frame-1 if piece.get('piece_type') in ACCESSORY else frame
    prefix=PREFIX.get(piece.get('piece_type'));candidates=[]
    if prefix and suffix is not None:
        names=[f'{prefix}{suffix}']
        if piece['piece_type']=='ShoeRightPiece':names.append(f'ShoeLeft{suffix}')
        if piece['piece_type']=='PantLeftPiece':names += [f'SkirtLeft{suffix}',f'ShortsLeft{suffix}',f'LegLeft{suffix}']
        if piece['piece_type']=='PantRightPiece':names += [f'SkirtRight{suffix}',f'ShortsRight{suffix}',f'LegRight{suffix}']
        for name in names:candidates.extend(class_index.get(name.lower(),[]))
    method='item_name_numeric_suffix' if match else ('frame_with_accessory_none_offset' if piece.get('piece_type') in ACCESSORY else 'frame_direct')
    return {'resolution_method':method,'expected_numeric_suffix':suffix,'resolved_symbols':candidates,'resolved':bool(candidates)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--xml-dir',required=True);parser.add_argument('--source-root',required=True);parser.add_argument('--output',required=True);args=parser.parse_args();xml_dir=Path(args.xml_dir);source_root=Path(args.source_root)
    docs=[]
    for path in sorted(xml_dir.rglob('*.xml')):
        source_file=path.with_suffix('.source.txt');source=source_file.read_text(errors='ignore').strip() if source_file.exists() else path.stem+'.swf'
        try:docs.append(SwfDoc(path,source))
        except Exception as exc:print('parse failure',path,exc,file=sys.stderr)
    symbols=[];class_index=defaultdict(list);poses={}
    for doc in docs:
        digest=None;swf_path=source_root/doc.source_swf
        if swf_path.exists():
            h=hashlib.sha256()
            with swf_path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
            digest=h.hexdigest()
        for cid,name in doc.symbols:
            category,confidence,evidence=classify(name);bounds=doc.bounds(cid);display=doc.display_list(cid)
            entry={'symbol_id':f'{doc.source_swf}::{name}','class_name':name,'character_id':cid,'source_swf':doc.source_swf,'source_swf_sha256':digest,'category':category,'classification_confidence':confidence,'classification_evidence':evidence,'heuristic_outfit_group':group_key(name),'local_bounds':rect_json(bounds),'display_list':display,'named_layers':[x for x in display if x.get('name')],'origin_px_in_trimmed_image':({'x':rnd(-bounds[0]/20),'y':rnd(-bounds[1]/20)} if bounds else None)}
            symbols.append(entry);class_index[name.lower()].append({'symbol_id':entry['symbol_id'],'class_name':name,'source_swf':doc.source_swf,'character_id':cid,'category':category,'local_bounds':entry['local_bounds']})
            slots=doc.slot_paths(cid)
            if slots and (name in BODY_WORDS or 'pose' in name.lower() or 'animation' in name.lower() or name=='VectorModelContainer'):
                poses[entry['symbol_id']]={'pose_name':name,'source_swf':doc.source_swf,'character_id':cid,'slots':slots}
    items=parse_items(source_root)
    for item in items:
        for piece in item['pieces']:piece['placement_resolution']=resolve_piece(item,piece,class_index)
    groups=defaultdict(lambda:{'symbols':[],'categories':set()})
    for symbol in symbols:
        if symbol['category']=='unclassified':continue
        key=f"{symbol['source_swf']}::{symbol['heuristic_outfit_group']}";groups[key]['symbols'].append(symbol['symbol_id']);groups[key]['categories'].add(symbol['category'])
    outfit_groups=[{'group_id':key,'source_swf':key.split('::')[0],'heuristic_key':key.split('::',1)[1],'symbols':value['symbols'],'categories':sorted(value['categories']),'confidence':'heuristic_name_grouping'} for key,value in groups.items() if len(value['symbols'])>1]
    data={'_meta':{'schema_version':'1.0.0','generated_by':'Pixie Hollow wearable placement extractor','coordinate_system':{'source_units':'SWF twips','twips_per_pixel':20,'matrix_order':'x=a*x+c*y+tx; y=b*x+d*y+ty','trimmed_png_usage':'Place the image so its local origin is at origin_px_in_trimmed_image, then apply the pose slot affine matrix.'},'important_model_note':'Items do not generally store a standalone x/y. Catalog records select one or more piece slots and a class/frame. Placement is the composition of wearable-symbol local authoring geometry with the selected avatar-pose slot matrix.','catalog_limitations':'Catalog item IDs are included wherever item XML exists in the archive. Every extractable wearable class symbol is included even when no item-ID record survives.','resolution_notes':['Legacy clothing frames map directly to numbered classes such as frame 11 -> Chest11.','Legacy accessories reserve frame 1 for none, so frame 12 -> Necklace11 and frame 9 -> Bracelet8.','Shoe artwork may use a ShoeLeft class for both left and right slots; pose transforms are preserved separately.','Later monthly bundles use descriptive class names and appear in wearable_symbols/outfit_groups even without an item ID.']},'statistics':{'swf_xml_documents':len(docs),'wearable_and_avatar_symbols':len(symbols),'pose_definitions':len(poses),'catalog_item_designs':len(items),'heuristic_outfit_groups':len(outfit_groups),'catalog_piece_records':sum(len(x['pieces']) for x in items),'resolved_catalog_piece_records':sum(p['placement_resolution']['resolved'] for x in items for p in x['pieces'])},'canonical_piece_slots':sorted(SLOT_WORDS),'poses':poses,'catalog_items':items,'wearable_symbols':symbols,'outfit_groups':sorted(outfit_groups,key=lambda x:x['group_id'])}
    Path(args.output).write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8');print(json.dumps(data['statistics'],indent=2));print('output bytes',Path(args.output).stat().st_size)
if __name__=='__main__':main()
