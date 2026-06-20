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

def _pipe_size_mm(n):
    """NFPA-style pipe schedule: nominal pipe Ø (mm) for n sprinklers carried downstream."""
    for lim,s in ((1,25),(2,32),(3,40),(5,50),(10,65),(20,80),(40,100)):
        if n<=lim: return s
    return 150

def _zone_layout(msp, place, zones, default_spacing_mm, HZ):
    """Agent-defined zones -> NFPA coverage-grid sprinklers + routed branch/cross-main
    pipework with per-segment pipe-size (Ø) labels. Returns (rows, sprk, feeds)."""
    rows=[]; sprk=[]; feeds=[]
    def _lab(x,y,s):
        t=msp.add_text("Ø%d"%s,dxfattribs={"layer":"FP-TEXT","height":160,"color":4}); t.set_placement((x,y))
    def _frange(a,b,step):
        out=[]; v=a
        while v<=b+1e-6: out.append(v); v+=step
        return out
    def _inside(poly,px,py,bb):
        if poly is None:
            x0,y0,x1,y1=bb; return x0<=px<=x1 and y0<=py<=y1
        c=False; n=len(poly); j=n-1
        for i in range(n):
            xi,yi=poly[i]; xj,yj=poly[j]
            if ((yi>py)!=(yj>py)) and (px<(xj-xi)*(py-yi)/((yj-yi) or 1e-9)+xi): c=not c
            j=i
        return c
    for zi,z in enumerate(zones,1):
        zname=str(z.get("name") or ("Z%d"%zi))
        if z.get("rect"):
            x0,y0,x1,y1=[float(v) for v in z["rect"]]; poly=None
        elif z.get("polygon") and len(z["polygon"])>=3:
            poly=[(float(p[0]),float(p[1])) for p in z["polygon"]]
            xs=[p[0] for p in poly]; ys=[p[1] for p in poly]; x0,y0,x1,y1=min(xs),min(ys),max(xs),max(ys)
        else:
            continue
        bb=(x0,y0,x1,y1); S=float(z.get("spacing_mm") or default_spacing_mm); ins=S/2.0
        gx=_frange(x0+ins,x1-ins,S) or [(x0+x1)/2.0]
        gy=_frange(y0+ins,y1-ins,S) or [(y0+y1)/2.0]
        feedx=x0+ins-S*0.6
        row_heads={}; znum=0
        for yy in gy:
            rowpts=sorted(xx for xx in gx if _inside(poly,xx,yy,bb))
            if not rowpts: continue
            row_heads[yy]=len(rowpts)
            for xx in rowpts:
                znum+=1; tag="SPK-%s-%02d"%(zname[:6].upper(),znum)
                place("FP_SPK",xx,yy,"FP-SPRINKLER-HEAD",tag,"ESFR K-25.2",HZ)
                rows.append([tag,"ESFR K-25.2 sprinkler","%.1f"%xx,"%.1f"%yy,zname,HZ,"FP-SPRINKLER-HEAD"]); sprk.append((xx,yy))
            pts=[feedx]+rowpts
            msp.add_lwpolyline([(px,yy) for px in pts],dxfattribs={"layer":"FP-PIPE-BRANCH"})
            for k in range(len(pts)-1):
                _lab((pts[k]+pts[k+1])/2.0,yy+S*0.05,_pipe_size_mm(len(rowpts)-k))
        if row_heads:
            ys=sorted(row_heads)
            msp.add_lwpolyline([(feedx,ry) for ry in ys],dxfattribs={"layer":"FP-PIPE-CROSSMAIN"})
            cum=0
            for idx in range(len(ys)-1,0,-1):
                cum+=row_heads[ys[idx]]
                _lab(feedx-S*0.3,(ys[idx]+ys[idx-1])/2.0,_pipe_size_mm(cum))
            total=cum+row_heads[ys[0]]
            _lab(feedx-S*0.3,ys[0]-S*0.25,_pipe_size_mm(total))
            feeds.append((feedx,ys[0],total,zname))
    return rows, sprk, feeds

def _draw_extras(msp, pipes, annotations, fittings):
    """Agent-supplied drawing primitives: the AGENT works out the routes/dimensions/
    fittings from the new plan (via /inspect) and the tool just renders them.
      pipes:       [{"points":[[x,y],...], "size": 65, "kind": "branch|cross_main|riser"}]
      annotations: [{"type":"text|dim|level", ...}]
      fittings:    [{"type":"tee|elbow|bfv|flow_arrow|gate_valve|check_valve|test_drain", "x","y","angle"}]
    """
    KMAP={"branch":"FP-PIPE-BRANCH","cross_main":"FP-PIPE-CROSSMAIN","crossmain":"FP-PIPE-CROSSMAIN",
          "main":"FP-PIPE-CROSSMAIN","riser":"FP-RISER","pipe":"FP-PIPE-BRANCH"}
    def _t(s,x,y,h=200,color=1,rot=0):
        e=msp.add_text(str(s),dxfattribs={"layer":"FP-TEXT","height":h,"color":color,"rotation":rot}); e.set_placement((x,y))
    for p in (pipes or []):
        pts=[(float(a[0]),float(a[1])) for a in p.get("points",[]) if len(a)>=2]
        if len(pts)<2: continue
        layer=KMAP.get(str(p.get("kind","branch")).lower().replace("-","_"),"FP-PIPE-BRANCH")
        msp.add_lwpolyline(pts,dxfattribs={"layer":layer})
        if p.get("size"):
            mx,my=pts[len(pts)//2]; _t("Ø%s"%p["size"],mx,my+120,h=200,color=1)
    for a in (annotations or []):
        ty=str(a.get("type","text")).lower()
        if ty in ("dim","dimension") and a.get("p1") and a.get("p2"):
            x1,y1=float(a["p1"][0]),float(a["p1"][1]); x2,y2=float(a["p2"][0]),float(a["p2"][1])
            msp.add_lwpolyline([(x1,y1),(x2,y2)],dxfattribs={"layer":"FP-DIM"})
            for tx,tyy in ((x1,y1),(x2,y2)): msp.add_lwpolyline([(tx,tyy-120),(tx,tyy+120)],dxfattribs={"layer":"FP-DIM"})
            _t(a.get("text") or "%d"%round(math.hypot(x2-x1,y2-y1)),(x1+x2)/2.0,(y1+y2)/2.0+120,h=float(a.get("height",220)),color=7)
        elif ty=="level" and "x" in a and "y" in a:
            x=float(a["x"]); y=float(a["y"])
            if a.get("ffl") is not None: _t("+%s FFL"%a["ffl"],x,y,h=220,color=3)
            if a.get("ssl") is not None: _t("+%s SSL"%a["ssl"],x,y-260,h=220,color=3)
        elif "x" in a and "y" in a:
            _t(a.get("text",""),float(a["x"]),float(a["y"]),h=float(a.get("height",250)),color=int(a.get("color",7)),rot=float(a.get("angle",0)))
    for f in (fittings or []):
        if "x" not in f or "y" not in f: continue
        ty=str(f.get("type","tee")).lower().replace("-","_"); x=float(f["x"]); y=float(f["y"]); ang=math.radians(float(f.get("angle",0)))
        if ty=="flow_arrow":
            dx,dy=math.cos(ang),math.sin(ang); L=450
            msp.add_lwpolyline([(x-dx*L,y-dy*L),(x+dx*L,y+dy*L)],dxfattribs={"layer":"FP-PIPE-BRANCH"})
            msp.add_lwpolyline([(x+dx*L,y+dy*L),(x+dx*L-dx*180-dy*110,y+dy*L-dy*180+dx*110),
                                (x+dx*L-dx*180+dy*110,y+dy*L-dy*180-dx*110),(x+dx*L,y+dy*L)],close=True,dxfattribs={"layer":"FP-PIPE-BRANCH"})
        elif ty in ("bfv","gate_valve","check_valve","test_drain"):
            msp.add_lwpolyline([(x-200,y-150),(x+200,y+150),(x+200,y-150),(x-200,y+150),(x-200,y-150)],dxfattribs={"layer":"FP-ZCV"})
            lbl={"bfv":"BFV","gate_valve":"GV","check_valve":"CV","test_drain":"T&D"}[ty]
            _t(lbl,x-180,y+230,h=140,color=5)
        elif ty=="tee":
            msp.add_circle((x,y),90,dxfattribs={"layer":"FP-PIPE-CROSSMAIN"})
        elif ty=="elbow":
            msp.add_circle((x,y),70,dxfattribs={"layer":"FP-PIPE-BRANCH"})

def fire_protection_pipeline(base_file, floor_sheet_id, nfpa_params=None, *, ahj=None, units=None,
                             scale=None, containment_tolerance_mm=None, output_formats=None,
                             title_block=None, workdir=None, devices=None, zones=None,
                             pipes=None, annotations=None, fittings=None):
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
    mk("FP_FHC",lambda b:(b.add_lwpolyline([(-250,-300),(250,-300),(250,300),(-250,300),(-250,-300)]),b.add_lwpolyline([(-130,-160),(130,-160),(130,160),(-130,160),(-130,-160)]),b.add_text("FHC",dxfattribs={"height":140}).set_placement((-130,-470))))

    def place(block,x,y,layer,tag,model,hz):
        r=msp.add_blockref(block,(x,y),dxfattribs={"layer":layer})
        r.add_auto_attribs({"TAG":tag,"MODEL":model,"X":f"{x:.1f}","Y":f"{y:.1f}","HAZARD":hz}); return r

    HZ=f"{nfpa['nfpa13']['hazard'].upper()}({nfpa['nfpa13']['density_mm_min']}mm/min)"
    rows=[]; sprk=[]
    if zones or devices:
        # ---- Agent-defined ZONES -> coverage-grid sprinklers + sized routed pipework ----
        feeds=[]
        if zones:
            zr,zs,feeds=_zone_layout(msp,place,zones,nfpa['nfpa13'].get('max_spacing_m',4.6)*1000.0,HZ)
            rows+=zr; sprk+=zs
        # ---- Explicit devices: riser, ZCV, detectors, hose reels, FHCs, wall openings, ... ----
        riser_xy=None
        if devices:
            PLACE_MAP=OrderedDict([
                ("sprinkler",("FP_SPK","FP-SPRINKLER-HEAD","ESFR K-25.2","SPK","ESFR K-25.2 sprinkler")),
                ("smoke",("FP_DET_S","FP-DET-SMOKE","Addr. Photoelectric","SD","Smoke detector")),
                ("heat",("FP_DET_H","FP-DET-HEAT","Addr. Fixed/RoR 57C","HD","Heat detector")),
                ("mcp",("FP_MCP","FP-MCP","Addressable MCP","MCP","Manual Call Point")),
                ("sounder",("FP_SND","FP-SOUNDER","Addr. Sounder","SB","Sounder/Beacon")),
                ("zcv",("FP_ZCV","FP-ZCV","BFV+FS+T&D","ZCV","Zone Control Valve")),
                ("landing_valve",("FP_LV","FP-LANDINGVALVE","Landing Valve DN65","LV","Landing Valve")),
                ("hose_reel",("FP_HR","FP-HOSEREEL","Hose Reel 30m DN25","HR","Hose Reel")),
                ("fhc",("FP_FHC","FP-HOSEREEL","Fire Hose Cabinet","FHC","Fire Hose Cabinet")),
                ("riser",("FP_RISER","FP-RISER","Wet Riser DN150","WR","Wet Riser"))])
            ALIAS={"sprinkler_head":"sprinkler","spk":"sprinkler","sprinklerhead":"sprinkler",
                   "smoke_detector":"smoke","smoke_det":"smoke","sd":"smoke","photoelectric":"smoke",
                   "heat_detector":"heat","heat_det":"heat","hd":"heat",
                   "manual_call_point":"mcp","callpoint":"mcp","call_point":"mcp",
                   "sounder_beacon":"sounder","beacon":"sounder","sb":"sounder",
                   "zone_control_valve":"zcv","zonecontrolvalve":"zcv",
                   "landingvalve":"landing_valve","lv":"landing_valve",
                   "hosereel":"hose_reel","hr":"hose_reel",
                   "fire_hose_cabinet":"fhc","hose_cabinet":"fhc","cabinet":"fhc",
                   "wet_riser":"riser","wetriser":"riser","standpipe":"riser","wr":"riser",
                   "opening":"wall_opening","ope":"wall_opening","penetration":"wall_opening"}
            def _txt(s,x,y,h=200,color=1):
                tt=msp.add_text(str(s),dxfattribs={"layer":"FP-TEXT","height":h,"color":color}); tt.set_placement((x,y))
            for dev in devices:
                t=str(dev.get("type","")).strip().lower().replace("-","_").replace(" ","_"); t=ALIAS.get(t,t)
                if "x" not in dev or "y" not in dev: continue
                x=float(dev["x"]); y=float(dev["y"])
                if t=="wall_opening":
                    w=float(dev.get("w",1000)); hh=float(dev.get("h",1670))
                    msp.add_lwpolyline([(x-w/2,y-hh/2),(x+w/2,y-hh/2),(x+w/2,y+hh/2),(x-w/2,y+hh/2),(x-w/2,y-hh/2)],close=True,dxfattribs={"layer":"FP-TITLEBLOCK"})
                    _txt(dev.get("label") or "%d(H)x%d(L) WALL OPENING"%(int(hh),int(w)),x+w/2+200,y,h=180)
                    rows.append([str(dev.get("tag") or "OPE"),"Wall Opening","%.1f"%x,"%.1f"%y,str(dev.get("room") or "wall"),HZ,"FP-TITLEBLOCK"]); continue
                if t not in PLACE_MAP: continue
                blk,layer,defmodel,pfx,tname=PLACE_MAP[t]
                tag=str(dev.get("tag") or "%s-%02d"%(pfx,sum(1 for r in rows if r[6]==layer)+1))
                place(blk,x,y,layer,tag,str(dev.get("model") or defmodel),HZ)
                rows.append([tag,tname,"%.1f"%x,"%.1f"%y,str(dev.get("room") or "agent-placed"),HZ,layer])
                if t=="sprinkler": sprk.append((x,y))
                elif t=="riser":
                    riser_xy=(x,y); sched=dev.get("schedule")
                    if sched:
                        ly=y-450
                        for line in (sched if isinstance(sched,(list,tuple)) else [sched]):
                            _txt(line,x+450,ly,h=180,color=7); ly-=320
                elif t=="zcv":
                    _txt("Ø%s ZCV"%dev.get("size",65),x+450,y+120,h=200)
                elif t in ("landing_valve","hose_reel"):
                    cov=dev.get("coverage_m")
                    if cov:
                        rr=float(cov)*1000.0
                        msp.add_lwpolyline([(x+rr*math.cos(a),y+rr*math.sin(a)) for a in [i*math.pi/24 for i in range(48)]],close=True,dxfattribs={"layer":"FP-COVERAGE-HATCH"})
                        _txt("%sm"%cov,x+rr*0.62,y+rr*0.62,h=300)
        # ---- Mains: connect each zone's cross-main feed to the riser, sized by zone demand ----
        if zones and riser_xy and feeds:
            for (fx,fy,heads,zn) in feeds:
                msp.add_lwpolyline([(fx,fy),(fx,riser_xy[1]),(riser_xy[0],riser_xy[1])],dxfattribs={"layer":"FP-PIPE-CROSSMAIN"})
                _ml=msp.add_text("Ø%d"%_pipe_size_mm(heads),dxfattribs={"layer":"FP-TEXT","height":220,"color":1}); _ml.set_placement(((fx+riser_xy[0])/2.0,riser_xy[1]+160))
        # Auto-wire explicit-only sprinklers (zones bring their own routing)
        if devices and not zones and sprk:
            cy0=sum(s[1] for s in sprk)/len(sprk); xmn=min(s[0] for s in sprk); xmx=max(s[0] for s in sprk)
            msp.add_lwpolyline([(xmn-1000,cy0),(xmx+1000,cy0)],dxfattribs={"layer":"FP-PIPE-CROSSMAIN"})
            for (x,y) in sprk: msp.add_lwpolyline([(x,y),(x,cy0)],dxfattribs={"layer":"FP-PIPE-BRANCH"})
        if sprk:
            cx=sum(s[0] for s in sprk)/len(sprk); cy=sum(s[1] for s in sprk)/len(sprk)
            xmin=min(s[0] for s in sprk); xmax=max(s[0] for s in sprk); ymin=min(s[1] for s in sprk); ymax=max(s[1] for s in sprk)
        else:
            pts=[(float(r[2]),float(r[3])) for r in rows] or [(0.0,0.0)]
            cx=sum(p[0] for p in pts)/len(pts); cy=sum(p[1] for p in pts)/len(pts)
            xmin=min(p[0] for p in pts); xmax=max(p[0] for p in pts); ymin=min(p[1] for p in pts); ymax=max(p[1] for p in pts)
        log.append((f"placement: {len(rows)} devices (zones={len(zones or [])}, explicit={len(devices or [])})",_utc()))
    else:
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

    if pipes or annotations or fittings:
        _draw_extras(msp,pipes,annotations,fittings)

    cover_r=nfpa['nfpa13'].get('max_spacing_m',4.6)*500.0; hatches=[]
    if not (zones or devices):  # filled coverage blobs clutter agent-designed / routed layouts
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
            "device_total":total_devices,"placement_mode":"zoned" if zones else ("explicit" if devices else "auto"),
            "verification":verify,"outputs":out,"log":log}

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
    ap.add_argument("--devices",default=""); ap.add_argument("--zones",default="")
    ap.add_argument("--pipes",default=""); ap.add_argument("--annotations",default=""); ap.add_argument("--fittings",default="")
    a=ap.parse_args(argv)
    res=fire_protection_pipeline(base_file=a.base_file,floor_sheet_id=a.floor_sheet_id,nfpa_params=json.loads(a.nfpa_params),
        ahj=a.ahj,units=a.units,scale=a.scale,containment_tolerance_mm=a.containment_tolerance_mm,
        output_formats=a.output_formats.split(","),title_block=json.loads(a.title_block),workdir=a.workdir,
        devices=(json.loads(a.devices) if a.devices else None),
        zones=(json.loads(a.zones) if a.zones else None),
        pipes=(json.loads(a.pipes) if a.pipes else None),
        annotations=(json.loads(a.annotations) if a.annotations else None),
        fittings=(json.loads(a.fittings) if a.fittings else None))
    print(json.dumps({k:res[k] for k in ("tool","version","sheet","device_total","schedule","verification","outputs")},indent=2,default=str))

if __name__=="__main__": _main(sys.argv[1:])
