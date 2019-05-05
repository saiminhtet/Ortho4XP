import enum
import datetime
import math
import os
import time
import shutil
import queue
import threading
import O4_UI_Utils as UI
import O4_File_Names as FNAMES
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Mask_Utils as MASK
import O4_DSF_Utils as DSF
import O4_Overlay_Utils as OVL
from O4_Parallel_Utils import parallel_launch, parallel_join
from O4_AirportDataSource import AirportDataSource, XPlaneTile
import shapely.geometry
import shapely.prepared

max_convert_slots=4 
skip_downloads=False
skip_converts=False

##############################################################################
def download_textures(tile,download_queue,convert_queue):
    UI.vprint(1,"-> Opening download queue.")
    done=0
    while True:
        texture_attributes=download_queue.get()
        if isinstance(texture_attributes,str) and texture_attributes=='quit':
            UI.progress_bar(2,100)
            break
        if IMG.build_jpeg_ortho(tile,*texture_attributes):
            done+=1
            UI.progress_bar(2,int(100*done/(done+download_queue.qsize()))) 
            convert_queue.put((tile,*texture_attributes))
        if UI.red_flag: UI.vprint(1,"Download process interrupted."); return 0
    if done: UI.vprint(1," *Download of textures completed.") 
    return 1
##############################################################################

##############################################################################
def build_tile(tile):
    if UI.is_working: return 0
    UI.is_working=1
    UI.red_flag=False
    UI.logprint("Step 3 for tile lat=",tile.lat,", lon=",tile.lon,": starting.")
    UI.vprint(0,"\nStep 3 : Building DSF/Imagery for tile "+FNAMES.short_latlon(tile.lat,tile.lon)+" : \n--------\n")
    
    if not os.path.isfile(FNAMES.mesh_file(tile.build_dir,tile.lat,tile.lon)):
        UI.lvprint(0,"ERROR: A mesh file must first be constructed for the tile!")
        UI.exit_message_and_bottom_line('')
        return 0

    timer=time.time()
    
    tile.write_to_config()
    
    if not IMG.initialize_local_combined_providers_dict(tile): 
        UI.exit_message_and_bottom_line('')
        return 0

    try:
        if not os.path.exists(os.path.join(tile.build_dir,'Earth nav data',FNAMES.round_latlon(tile.lat,tile.lon))):
            os.makedirs(os.path.join(tile.build_dir,'Earth nav data',FNAMES.round_latlon(tile.lat,tile.lon)))
        if not os.path.isdir(os.path.join(tile.build_dir,'textures')):
            os.makedirs(os.path.join(tile.build_dir,'textures'))
        if UI.cleaning_level>1 and not tile.grouped:
            for f in os.listdir(os.path.join(tile.build_dir,'textures')):
                if f[-4:]!='.png': continue
                try: os.remove(os.path.join(tile.build_dir,'textures',f))
                except: pass
        if not tile.grouped:    
            try: shutil.rmtree(os.path.join(tile.build_dir,'terrain'))
            except: pass
        if not os.path.isdir(os.path.join(tile.build_dir,'terrain')):
            os.makedirs(os.path.join(tile.build_dir,'terrain'))
    except Exception as e: 
        UI.lvprint(0,"ERROR: Cannot create tile subdirectories.")
        UI.vprint(3,e)
        UI.exit_message_and_bottom_line('')
        return 0
    
    download_queue=queue.Queue()
    convert_queue=queue.Queue()
    build_dsf_thread=threading.Thread(target=DSF.build_dsf,args=[tile,download_queue])
    download_thread=threading.Thread(target=download_textures,args=[tile,download_queue,convert_queue])
    build_dsf_thread.start()
    if not skip_downloads:
        download_thread.start()
        if not skip_converts:
            UI.vprint(1,"-> Opening convert queue and",max_convert_slots,"conversion workers.")
            dico_conv_progress={'done':0,'bar':3}
            convert_workers=parallel_launch(IMG.convert_texture,convert_queue,max_convert_slots,progress=dico_conv_progress)
    build_dsf_thread.join()
    if not skip_downloads:
        download_queue.put('quit')
        download_thread.join()
        if not skip_converts:
            for _ in range(max_convert_slots): convert_queue.put('quit')
            parallel_join(convert_workers) 
            if UI.red_flag: 
                UI.vprint(1,"DDS conversion process interrupted.")
            elif dico_conv_progress['done']>=1: 
                UI.vprint(1," *DDS conversion of textures completed.")
    UI.vprint(1," *Activating DSF file.")
    dsf_file_name=os.path.join(tile.build_dir,'Earth nav data',FNAMES.long_latlon(tile.lat,tile.lon)+'.dsf')
    try:
        os.rename(dsf_file_name+'.tmp',dsf_file_name)
    except:
        UI.vprint(0,"ERROR : could not rename DSF file, tile is not actived.")
    if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
    if UI.cleaning_level>1:
        try: os.remove(FNAMES.alt_file(tile))
        except: pass
        try: os.remove(FNAMES.input_node_file(tile))
        except: pass
        try: os.remove(FNAMES.input_poly_file(tile))
        except: pass
    if UI.cleaning_level>2:
        try: os.remove(FNAMES.mesh_file(tile.build_dir,tile.lat,tile.lon))
        except: pass
        try: os.remove(FNAMES.apt_file(tile))
        except: pass
    if UI.cleaning_level>1 and not tile.grouped:
        remove_unwanted_textures(tile)
    UI.timings_and_bottom_line(timer)
    UI.logprint("Step 3 for tile lat=",tile.lat,", lon=",tile.lon,": normal exit.")
    return 1
##############################################################################

##############################################################################
def build_all(tile):
    VMAP.build_poly_file(tile)
    if UI.red_flag: UI.exit_message_and_bottom_line(''); return 0
    MESH.build_mesh(tile)
    if UI.red_flag: UI.exit_message_and_bottom_line(''); return 0
    MASK.build_masks(tile)
    if UI.red_flag: UI.exit_message_and_bottom_line(''); return 0
    build_tile(tile)
    if UI.red_flag: UI.exit_message_and_bottom_line(''); return 0
    UI.is_working=0
    return 1
##############################################################################

##############################################################################
def build_tile_list(tile,list_lat_lon,do_osm,do_mesh,do_mask,do_dsf,do_ovl,do_ptc):
    if UI.is_working: return 0
    UI.red_flag=0
    timer=time.time()
    UI.lvprint(0,"Batch build launched for a number of",len(list_lat_lon),"tiles.")

    wall_time = time.clock()
    UI.lvprint(0,"Auto-generating a list of ZL zones around the airports of each tile.")
    zone_lists = smart_zone_list(list_lat_lon=list_lat_lon,
                                 screen_res=ScreenRes.OcculusRift,
                                 fov=60,
                                 fpa=10,
                                 provider='GO2',
                                 max_zl=19,
                                 min_zl=16,
                                 greediness=3,
                                 greediness_threshold=0.70)
    wall_time_delta = datetime.timedelta(seconds=(time.clock() - wall_time))
    UI.lvprint(0, "ZL zones computed in {}s".format(wall_time_delta))

    for (k, (lat, lon)) in enumerate(list_lat_lon):
        UI.vprint(1,"Dealing with tile ",k+1,"/",len(list_lat_lon),":",FNAMES.short_latlon(lat,lon)) 
        (tile.lat,tile.lon)=(lat,lon)
        tile.build_dir=FNAMES.build_dir(tile.lat,tile.lon,tile.custom_build_dir)
        tile.dem=None
        if do_ptc: tile.read_from_config()
        tile.zone_list = zone_lists[k]
        if (do_osm or do_mesh or do_dsf): tile.make_dirs()
        if do_osm: 
            VMAP.build_poly_file(tile)
            if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
        if do_mesh: 
            MESH.build_mesh(tile)
            if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
        if do_mask: 
            MASK.build_masks(tile)
            if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
        if do_dsf: 
            build_tile(tile)
            if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
        if do_ovl: 
            OVL.build_overlay(lat,lon)
            if UI.red_flag: UI.exit_message_and_bottom_line(); return 0
        try:
            UI.gui.earth_window.canvas.delete(UI.gui.earth_window.dico_tiles_todo[(lat,lon)]) 
            UI.gui.earth_window.dico_tiles_todo.pop((lat,lon),None)
        except Exception as e:
            print(e)
    UI.lvprint(0,"Batch process completed in",UI.nicer_timer(time.time()-timer))
    return 1
##############################################################################

##############################################################################
def remove_unwanted_textures(tile):
    texture_list=[]
    for f in os.listdir(os.path.join(tile.build_dir,'terrain')):
        if f[-4:]!='.ter': continue
        if f[-5]!='y':  #overlay
            texture_list.append(f.replace('.ter','.dds'))
        else:
            texture_list.append('_'.join(f[:-4].split('_')[:-2])+'.dds')
    for f in os.listdir(os.path.join(tile.build_dir,'textures')):   
        if f[-4:]!='.dds': continue
        if f not in texture_list:
            print("Removing obsolete texture",f)
            try: os.remove(os.path.join(tile.build_dir,'textures',f))
            except:pass
##############################################################################


class ScreenRes(enum.Enum):
    _720p = (1280, 720)
    SD = _720p
    _1080p = (1920, 1080)
    HD = _1080p
    _1440p = (2560, 1440)
    QHD = _1440p
    _2160p = (3840, 2160)
    _4K = _2160p
    _4320p = (7680, 4320)
    _8K = _4320p
    OcculusRift = (1080, 1200)  # Per eye


def smart_zone_list(list_lat_lon, screen_res, fov, fpa, provider, max_zl, min_zl, greediness=1, greediness_threshold=0.70):
    tiles_to_build = [XPlaneTile(lat, lon) for (lat, lon) in list_lat_lon]
    airport_collection = AirportDataSource().airports_in(tiles_to_build, include_surrounding_tiles=True)

    all_zones = []
    for tile in tiles_to_build:
        tile_poly = shapely.prepared.prep(tile.polygon())
        tile_zones = []
        for zl in range(max_zl, min_zl - 1, -1):
            for polygon in airport_collection.polygons(zl,
                                                       max_zl,
                                                       screen_res.value[0] if isinstance(screen_res, ScreenRes) else screen_res,
                                                       fov,
                                                       fpa,
                                                       greediness,
                                                       greediness_threshold):
                if not tile_poly.disjoint(polygon):
                    coords = []
                    for (x, y) in polygon.exterior.coords:
                        coords.extend([y, x])
                    tile_zones.append([coords, zl, provider])
        all_zones.append(tile_zones)
    return all_zones


def smart_zone_list_1(tile_lat_lon, screen_res, fov, fpa, provider, max_zl, min_zl, greediness=1, greediness_threshold=0.70):
    return smart_zone_list([tile_lat_lon], screen_res, fov, fpa, provider, max_zl, min_zl, greediness, greediness_threshold)[0]
