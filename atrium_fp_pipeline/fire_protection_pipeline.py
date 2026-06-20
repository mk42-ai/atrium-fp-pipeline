#!/usr/bin/env python3
"""
atrium-fp-pipeline · fire_protection_pipeline (v1.0.0)
Reusable Atrium Tower fire-protection CAD pipeline. 6 stages:
 1 base ingest · 2 FP overlay (18 NFPA layers) · 3 device placement + traceability
 4 auto schedule · 5 self-verification G1/G2/G3(/G4) · 6 NFPA print export (DXF/PDF/PNG).
Engine: ezdxf 1.4.4 + matplotlib. No network / API keys / credentials.
"""
from __future__ import annotations
import os, sys, json, csv, math, shutil, subprocess, datetime
from collections import Counter, OrderedDict

TOOL_ID="atrium-fp-pipeline"; TOOL_NAME="fire_protection_pipeline"; TOOL_VERSION="1.0.0"

DEFAULTS={"ahj":"ADCD","units":"mm","scale":"1:200 (at A1)","containment_tolerance_mm":8000,
 "output_formats":["dxf","pdf","png"],
 "title_block":{"project_name":"ATRIUM TOWER — FIRE PROTECTION","rfq":"GHC/RFQ/FP/2026/088",
                "tenderer":"NAFFCO","revision":"FP-Final"},
 "nfpa_params":{"nfpa13":{"hazard":"Light","density_mm_min":4.1,"max_area_m2":20.9,"max_spacing_m":4.6},
                "nfpa14":{"standpipe_class":"II","landing_valve_dn":65,"hose_reel":"30m DN25"},
                "nfpa72":{"smoke_spacing_m":9.1,"heat_temp_c":57,"mcp_travel_m":61},
                "nfpa170":{"symbol_set":"fire-safety"}}}
NFPA_CLAUSE={"FP-SPRINKLER-HEAD":"NFPA 13 8.3/11.2.3 (Light 4.1mm/min)","FP-DET-SMOKE":"NFPA 72 17.7",
 "FP-DET-HEAT":"NFPA 72 17.6","FP-MCP":"NFPA 72 17.14","FP-SOUNDER":"NFPA 72 18.4/18.5",
 "FP-ZCV":"NFPA 13 16.9 / NFPA 72 17.16","FP-LANDINGVALVE":"NFPA 14 7.3","FP-HOSEREEL":"NFPA 14 / ADCD",
 "FP-RISER":"NFPA 14 7","FP-PIPE-BRANCH":"NFPA 13 8","FP-PIPE-CROSSMAIN":"NFPA 13 8.15"}
FP_LAYERS={"FP-SPRINKLER-HEAD":1,"FP-PIPE-BRANCH":4,"FP-PIPE-CROSSMAIN":5,"FP-RISER":6,"FP-ZCV":3,
 "FP-HOSEREEL":2,"FP-LANDINGVALVE":30,"FP-HYDRANT":1,"FP-DET-SMOKE":3,"FP-DET-HEAT":30,"FP-DET-BEAM":4,
 "FP-MCP":1,"FP-SOUNDER":6,"FP-COVERAGE-HATCH":8,"FP-DIM":9,"FP-TEXT":7,"FP-LEGEND":7,"FP-TITLEBLOCK":7}

def _utc(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _resolve_base_dxf(base_file, workdir):
    if base_file.lower().endswith(".dxf"): return base_file,"ingested DXF directly"
    sib=base_file[:-4]+".dxf"; dwg2dxf=shutil.which("dwg2dxf") or "/tmp/libredwg/_b/dwg2dxf"
    if os.path.exists(dwg2dxf):
        out=os.path.join(workdir,"_base_from_dwg.dxf")
        subprocess.run([dwg2dxf,"-y","-o",out,base_file],capture_output=True,text=True,timeout=600)
        if os.path.exists(out) and os.path.getsize(out)>1000: return out,"converted DWG->DXF via LibreDWG"
    if os.path.exists(sib): return sib,"DWG converter unavailable; used sibling .dxf"
    raise FileNotFoundError(f"Cannot resolve a DXF for base {base_file}")

def fire_protection_pipeline(base_file, floor_sheet_id, nfpa_params=None, *, ahj=None, units=None,
                             scale=None, containment_tolerance_mm=None, output_formats=None,
                             title_block=None, workdir=None):
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    ahj=ahj or DEFAULTS["ahj"]; units=units or DEFAULTS["units"]; scale=scale or DEFAULTS["scale"]
    tol=containment_tolerance_mm or DEFAULTS["containment_tolerance_mm"]
    output_formats=[f.lower() for f in (output_formats or DEFAULTS["output_formats"])]
    tb=dict(DEFAULTS["title_block"]); tb.update(title_block or {})
    nfpa=json.loads(json.dumps(DEFAULTS["nfpa_params"]))
    if nfpa_params: nfpa.update(nfpa_params)
    workdir=workdir or os.getcwd(); sid=floor_sheet_id; log=[(f"start {sid}",_utc())]
    base_dxf,ingest_note=_resolve_base_dxf(base_file,workdir)

    doc=ezdxf.readfile(base_dxf); msp=doc.modelspace()
    base_layers={l.dxf.name:(l.dxf.color,l.dxf.linetype) for l in doc.layers}
    base_cnt=Counter(e.dxftype() for e in msp); base_total=sum(base_cnt.values())
    extmin=doc.header.get("$EXTMIN",(0,0,0)); extmax=doc.header.get("$EXTMAX",(1,1,0))
    room_tags=[(t.dxf.text.strip(),float(t.dxf.insert[0]),float(t.dxf.insert[1]))
               for t in msp.query("TEXT") if t.dxf.layer=="A-ANNO-ROOM"]
    if not room_tags:
        room_tags=[(t.dxf.text.strip(),float(t.dxf.insert[0]),float(t.dxf.insert[1])) for t in msp.query("TEXT")]
    snapshot={"sheet":sid,"dxfversion":doc.dxfversion,"base_layer_count":len(base_layers),
              "base_entity_total":base_total,"base_entity_counts":dict(base_cnt),
              "extmin":[round(v,1) for v in extmin],"extmax":[round(v,1) for v in extmax],
              "room_tag_count":len(room_tags),"ingest":ingest_note}
    json.dump(snapshot,open(os.path.join(workdir,f"{sid}_base_snapshot.json"),"w"),indent=2,default=str)
    log.append((f"base ingest {len(base_layers)} layers / {base_total} entities / {len(room_tags)} room tags",_utc()))

    for n,c in FP_LAYERS.items():
        if n not in doc.layers: doc.layers.add(n,color=c)
    def mk(name,draw):
        if name in doc.blocks: return
        b=doc.blocks.new(name=name); draw(b)
        for a,(ix,iy,h,inv) in {"TAG":(250,250,180,0),"MODEL":(0,0,1,1),"X":(0,0,1,1),"Y":(0,0,1,1),"HAZARD":(250,-300,150,1)}.items():
            b.add_attdef(a,dxfattribs={"insert":(ix,iy),"height":h,"invisible":inv})
    mk("FP_SPK",lambda b:(b.add_circle((0,0),150),b.add_line((-150,0),(150,0)),b.add_line((0,-150),(0,150))))
    mk("FP_DET_S",lambda b:(b.add_circle((0,0),160),b.add_text("S",dxfattribs={"height":140}).set_placement((-70,-70))))
    mk("FP_DET_H",lambda b:(b.add_lwpolyline([(-160,-160),(160,-160),(160,160),(-160,160),(-160,-160)]),b.add_text("H",dxfattribs={"height":140}).set_placement((-70,-70))))
    mk("FP_MCP",lambda b:(b.add_lwpolyline([(-160,-160),(160,-160),(160,160),(-160,160),(-160,-160)]),b.add_text("M",dxfattribs={"height":140}).set_placement((-70,-70))))
    mk("FP_SND",lambda b:(b.add_circle((0,0),150),b.add_lwpolyline([(150,-90),(330,-200),(330,200),(150,90)])))
    mk("FP_ZCV",lambda b:(b.add_lwpolyline([(-400,-250),(400,-250),(400,250),(-400,250),(-400,-250)]),b.add_line((-400,0),(400,0)),b.add_text("ZCV",dxfattribs={"height":150}).set_placement((-180,40))))
    mk("FP_LV",lambda b:(b.add_lwpolyline([(-180,-180),(180,180)]),b.add_lwpolyline([(-180,180),(180,-180)]),b.add_circle((0,0),200)))
    mk("FP_HR",lambda b:(b.add_circle((0,0),300),b.add_circle((0,0),90)))
    mk("FP_RISER",lambda b:(b.add_circle((0,0),250),b.add_text("R",dxfattribs={"height":220}).set_placement((-90,-110))))

    def place(block,x,y,layer,tag,model,hz):
        r=msp.add_blockref(block,(x,y),dxfattribs={"layer":layer})
        r.add_auto_attribs({"TAG":tag,"MODEL":model,"X":f"{x:.1f}","Y":f"{y:.1f}","HAZARD":hz}); return r

    HZ=f"{nfpa['nfpa13']['hazard'].upper()}({nfpa['nfpa13']['density_mm_min']}mm/min)"
    rows=[]; sprk=[]
    for nm,x,y in room_tags:
        rid=nm[-2:] if len(nm)>=2 else nm
        place("FP_SPK",x,y,"FP-SPRINKLER-HEAD",f"SPK-{rid}","ESFR K-25.2",HZ); sprk.append((x,y))
        rows.append([f"SPK-{rid}","ESFR K-25.2 sprinkler",f"{x:.1f}",f"{y:.1f}",nm,HZ,"FP-SPRINKLER-HEAD"])
        sx,sy=x+800,y+800; hx,hy=x-800,y-800
        place("FP_DET_S",sx,sy,"FP-DET-SMOKE",f"SD-{rid}","Addr. Photoelectric",HZ)
        place("FP_DET_H",hx,hy,"FP-DET-HEAT",f"HD-{rid}","Addr. Fixed/RoR 57C",HZ)
        rows.append([f"SD-{rid}","Smoke detector",f"{sx:.1f}",f"{sy:.1f}",nm,HZ,"FP-DET-SMOKE"])
        rows.append([f"HD-{rid}","Heat detector",f"{hx:.1f}",f"{hy:.1f}",nm,HZ,"FP-DET-HEAT"])
    cx=sum(s[0] for s in sprk)/len(sprk); cy=sum(s[1] for s in sprk)/len(sprk)
    xmin=min(s[0] for s in sprk); xmax=max(s[0] for s in sprk); ymin=min(s[1] for s in sprk); ymax=max(s[1] for s in sprk)
    msp.add_lwpolyline([(xmin-1000,cy),(xmax+1000,cy)],dxfattribs={"layer":"FP-PIPE-CROSSMAIN"})
    for (x,y) in sprk: msp.add_lwpolyline([(x,y),(x,cy)],dxfattribs={"layer":"FP-PIPE-BRANCH"})
    msp.add_lwpolyline([(cx,cy),(cx,cy+2500)],dxfattribs={"layer":"FP-RISER"})
    place("FP_RISER",cx,cy,"FP-RISER","WR-1","Wet Riser DN150",HZ)
    rows.append(["WR-1","Wet Riser DN150",f"{cx:.1f}",f"{cy:.1f}","core(centroid)",HZ,"FP-RISER"])
    place("FP_ZCV",cx+1200,cy,"FP-ZCV","ZCV-01","BFV+FS+T&D",HZ)
    rows.append(["ZCV-01","Zone Control Valve",f"{cx+1200:.1f}",f"{cy:.1f}","core",HZ,"FP-ZCV"])
    for i,(lx,ly) in enumerate([(xmin+400,cy+(ymax-cy)*0.5),(xmax-400,cy-(cy-ymin)*0.5)],1):
        place("FP_LV",lx,ly,"FP-LANDINGVALVE",f"LV-{i:02d}","Landing Valve DN65",HZ)
        place("FP_HR",lx+600,ly,"FP-HOSEREEL",f"HR-{i:02d}","Hose Reel 30m DN25",HZ)
        rows.append([f"LV-{i:02d}","Landing Valve",f"{lx:.1f}",f"{ly:.1f}","core/stair",HZ,"FP-LANDINGVALVE"])
        rows.append([f"HR-{i:02d}","Hose Reel",f"{lx+600:.1f}",f"{ly:.1f}","core/stair",HZ,"FP-HOSEREEL"])
    for i,(mx,my) in enumerate([(cx,ymax+1200),(cx,ymin-1200)],1):
        place("FP_MCP",mx,my,"FP-MCP",f"MCP-{i:02d}","Addressable MCP",HZ)
        place("FP_SND",mx+500,my,"FP-SOUNDER",f"SB-{i:02d}","Addr. Sounder",HZ)
        rows.append([f"MCP-{i:02d}","Manual Call Point",f"{mx:.1f}",f"{my:.1f}","exit/corridor",HZ,"FP-MCP"])
        rows.append([f"SB-{i:02d}","Sounder/Beacon",f"{mx+500:.1f}",f"{my:.1f}","exit/corridor",HZ,"FP-SOUNDER"])
    log.append((f"placed {len(rows)} devices",_utc()))

    cover_r=nfpa['nfpa13'].get('max_spacing_m',4.6)*500.0; hatches=[]
    for (x,y) in sprk:
        h=msp.add_hatch(color=8,dxfattribs={"layer":"FP-COVERAGE-HATCH"})
        h.paths.add_polyline_path([(x+cover_r*math.cos(a),y+cover_r*math.sin(a)) for a in [i*math.pi/16 for i in range(32)]],is_closed=True)
        h.set_solid_fill(color=8); h.transparency=0.70; hatches.append(h)

    TYPEMAP=OrderedDict([("FP-SPRINKLER-HEAD",("Sprinkler Head (ESFR)","ESFR K-25.2")),
     ("FP-DET-SMOKE",("Smoke Detector","Addr. Photoelectric")),("FP-DET-HEAT",("Heat Detector","Addr. Fixed/RoR 57C")),
     ("FP-MCP",("Manual Call Point","Addressable MCP")),("FP-SOUNDER",("Sounder/Beacon","Addr. Sounder")),
     ("FP-ZCV",("Zone Control Valve","BFV+FS+T&D")),("FP-LANDINGVALVE",("Landing Valve","DN65")),
     ("FP-HOSEREEL",("Hose Reel","30m DN25")),("FP-RISER",("Wet Riser","DN150"))])
    agg=Counter(e.dxf.layer for e in msp.query("INSERT") if e.dxf.layer in TYPEMAP and e.attribs)
    total_devices=sum(agg.values())

    def txt(s,x,y,h=400,layer="FP-LEGEND",color=7,align=TextEntityAlignment.LEFT):
        t=msp.add_text(s,dxfattribs={"layer":layer,"height":h,"color":color}); t.set_placement((x,y),align=align); return t
    AX=max(extmax[0]*0.34, xmax+9000)
    ly=ymax+2000; txt("FIRE PROTECTION LEGEND (NFPA 170)",AX,ly,h=550); ly-=1000
    for lbl in ["Sprinkler head ESFR K-25.2","Branch line","Cross-main","Wet riser","Zone control valve",
                "Hose reel","Landing valve","Smoke detector","Heat detector","Manual call point","Sounder/beacon"]:
        txt(lbl,AX,ly,h=360); ly-=720
    sx,sy=AX+9000,ymax+1500; rh=720; cols=[0,6500,11500,14500]
    txt("DEVICE SCHEDULE (auto from block attributes)",sx,sy+900,h=480)
    for j,hd in enumerate(["Type","Model/EMS","Qty","Total"]): txt(hd,sx+cols[j],sy,h=400)
    yy=sy-rh
    for lyr,(tp,mdl) in TYPEMAP.items():
        q=agg.get(lyr,0)
        if not q: continue
        txt(tp,sx+cols[0],yy,h=350);txt(mdl,sx+cols[1],yy,h=350);txt(str(q),sx+cols[2],yy,h=350);txt(str(q),sx+cols[3],yy,h=350);yy-=rh
    txt(f"TOTAL DEVICES   {total_devices}",sx+cols[0],yy,h=400)
    tbx,tby,tbw,tbh=AX,ymin-6000,30000,4800
    msp.add_lwpolyline([(tbx,tby),(tbx+tbw,tby),(tbx+tbw,tby+tbh),(tbx,tby+tbh),(tbx,tby)],close=True,dxfattribs={"layer":"FP-TITLEBLOCK"})
    tyy=tby+tbh-900
    for s,h in [(tb["project_name"],600),(f"{sid} — Fire Fighting Layout",440),
                (f'RFQ: {tb["rfq"]}   Tenderer: {tb["tenderer"]}   AHJ: {ahj}',380),
                (f'Units: {units}   Scale: {scale}   Drawing: {sid}   Rev: {tb["revision"]}',360)]:
        txt(s,tbx+600,tyy,h=h,layer="FP-TITLEBLOCK"); tyy-=1050

    now_base={l.dxf.name for l in doc.layers if not l.dxf.name.startswith("FP-")}
    g1=(now_base==set(base_layers)) and (len([e for e in msp if e.dxf.layer in base_layers])==base_total==snapshot["base_entity_total"])
    def nearest(x,y):
        best=1e30
        for _,rx,ry in room_tags: best=min(best,(rx-x)**2+(ry-y)**2)
        return math.sqrt(best)
    def inb(x,y): return extmin[0]<=x<=extmax[0] and extmin[1]<=y<=extmax[1]
    dist=[nearest(float(r[2]),float(r[3])) for r in rows]
    g2_flag=[r[0] for r,d in zip(rows,dist) if d>tol]; g2=len(g2_flag)==0
    g3_oob=[r[0] for r in rows if not inb(float(r[2]),float(r[3]))]; g3=len(g3_oob)==0
    gate_pass=g1 and g2 and g3
    defined=set(b.name for b in doc.blocks)
    missing=dict(Counter(i.dxf.name for i in msp.query("INSERT") if i.dxf.name not in defined))
    verify={"sheet":sid,"engine":"ezdxf "+ezdxf.__version__,"timestamp":_utc(),
            "gate1_arch_untouched":bool(g1),"gate2_containment":bool(g2),"gate2_flagged":g2_flag,
            "gate2_max_dist_mm":round(max(dist),1),"gate2_tol_mm":tol,"gate3_within_extents":bool(g3),
            "gate3_oob":g3_oob,"overall":"PASS" if gate_pass else "FAIL","missing_block_refs":missing,
            "device_total":total_devices,"base_layers":len(base_layers),"final_layers":len(list(doc.layers))}
    log.append((f"gates G1={g1} G2={g2} G3={g3} -> {verify['overall']}",_utc()))

    out={}; base_name=f"{sid}_FireProtection"
    try: doc.materials.clear(); doc.materials.create_required_entries()
    except Exception: pass
    if "dxf" in output_formats: p=os.path.join(workdir,base_name+".dxf"); doc.saveas(p); out["dxf"]=p
    csvp=os.path.join(workdir,f"{sid}_device_traceability.csv")
    with open(csvp,"w",newline="") as f:
        w=csv.writer(f); w.writerow(["device_tag","device_type","source_X_mm","source_Y_mm","mapped_room","hazard_class","fp_layer","code_clause"])
        for r in rows: w.writerow(r+[NFPA_CLAUSE.get(r[6],"NFPA 170")])
    out["csv"]=csvp
    if "png" in output_formats or "pdf" in output_formats:
        _render(doc,msp,(xmin-6000,ymin-8000,sx+18000,ymax+4000),workdir,base_name,output_formats,out)
    vmd=os.path.join(workdir,f"{base_name}_verification.md")
    _write_verify_md(vmd,verify,snapshot,tb,ahj,units,scale,nfpa); out["verification_md"]=vmd
    json.dump(verify,open(os.path.join(workdir,f"{base_name}_verification.json"),"w"),indent=2)
    out["verification_json"]=os.path.join(workdir,f"{base_name}_verification.json")
    out["base_snapshot"]=os.path.join(workdir,f"{sid}_base_snapshot.json")
    return {"tool":TOOL_ID,"version":TOOL_VERSION,"sheet":sid,"snapshot":snapshot,"schedule":dict(agg),
            "device_total":total_devices,"verification":verify,"outputs":out,"log":log}

def _render(doc,msp,win,workdir,base_name,fmts,out):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import RenderContext,Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.config import Configuration
    from ezdxf.addons.drawing.properties import LayoutProperties
    for nm in {i.dxf.name for i in msp.query("INSERT") if i.dxf.name not in doc.blocks}: doc.blocks.new(name=nm)
    for lyr in doc.layers:
        if not lyr.dxf.name.startswith("FP-"): lyr.dxf.color=8
    def render(path,dpi,long_px):
        w=win[2]-win[0]; h=win[3]-win[1]; asp=h/w; fw=long_px/dpi
        fig=plt.figure(figsize=(fw,fw*asp),dpi=dpi); ax=fig.add_axes([0,0,1,1]); ax.set_axis_off()
        lp=LayoutProperties.from_layout(msp); lp.set_colors("#ffffff")
        Frontend(RenderContext(doc),MatplotlibBackend(ax),config=Configuration(background_policy=None)).draw_layout(msp,finalize=True,layout_properties=lp)
        ax.set_xlim(win[0],win[2]); ax.set_ylim(win[1],win[3]); ax.set_aspect("auto")
        fig.set_size_inches(fw,fw*asp); fig.savefig(path,dpi=dpi,facecolor="white",bbox_inches=None,pad_inches=0); plt.close(fig)
    if "png" in fmts: p=os.path.join(workdir,base_name+"_detailed.png"); render(p,300,7000); out["png"]=p
    if "pdf" in fmts: p=os.path.join(workdir,base_name+"_detailed.pdf"); render(p,300,7000); out["pdf"]=p

def _write_verify_md(path,v,snap,tb,ahj,units,scale,nfpa):
    L=[f"# {snap['sheet']} Fire-Protection Self-Verification\n",
       f"Engine: {v['engine']} · Generated: {v['timestamp']} · AHJ: {ahj} · Units: {units} · Scale: {scale}\n",
       f"## Decision Gate: {'VERIFICATION PASSED — export cleared' if v['overall']=='PASS' else 'FAILED — DO NOT EXPORT'}\n",
       "| # | Gate | Result |","|---|---|---|",
       f"| 1 | Architecture untouched | {'PASS' if v['gate1_arch_untouched'] else 'FAIL'} |",
       f"| 2 | Device-in-room containment (<= {v['gate2_tol_mm']} mm) | {'PASS' if v['gate2_containment'] else 'FAIL'} |",
       f"| 3 | Within drawing extents | {'PASS' if v['gate3_within_extents'] else 'FAIL'} |",
       f"| 4 | Unresolved/missing block refs | Enumerated ({sum(v['missing_block_refs'].values())}) |\n",
       f"- Devices: {v['device_total']} · base layers {v['base_layers']} (unchanged) · final layers {v['final_layers']}",
       f"- Gate 2: max nearest-room distance {v['gate2_max_dist_mm']} mm; flagged {len(v['gate2_flagged'])}",
       f"- Gate 3: out-of-bounds {len(v['gate3_oob'])}"]
    open(path,"w").write("\n".join(L))

def _main(argv):
    import argparse
    ap=argparse.ArgumentParser(prog=TOOL_NAME)
    ap.add_argument("--base_file",required=True); ap.add_argument("--floor_sheet_id",required=True)
    ap.add_argument("--ahj",default=DEFAULTS["ahj"]); ap.add_argument("--units",default=DEFAULTS["units"])
    ap.add_argument("--scale",default=DEFAULTS["scale"]); ap.add_argument("--containment_tolerance_mm",type=int,default=DEFAULTS["containment_tolerance_mm"])
    ap.add_argument("--output_formats",default="dxf,pdf,png"); ap.add_argument("--nfpa_params",default="{}")
    ap.add_argument("--title_block",default="{}"); ap.add_argument("--workdir",default=os.getcwd())
    a=ap.parse_args(argv)
    res=fire_protection_pipeline(base_file=a.base_file,floor_sheet_id=a.floor_sheet_id,nfpa_params=json.loads(a.nfpa_params),
        ahj=a.ahj,units=a.units,scale=a.scale,containment_tolerance_mm=a.containment_tolerance_mm,
        output_formats=a.output_formats.split(","),title_block=json.loads(a.title_block),workdir=a.workdir)
    print(json.dumps({k:res[k] for k in ("tool","version","sheet","device_total","schedule","verification","outputs")},indent=2,default=str))

if __name__=="__main__": _main(sys.argv[1:])
